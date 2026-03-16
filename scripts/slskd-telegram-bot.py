#!/usr/bin/env python3
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

RECOVER_PATH = "/usr/local/bin/slskd-recover.py"
LOG_FILE = "/var/log/slskd-telegram-bot.log"
STATE_DIR = Path("/var/lib/slskd-telegram-bot")
OFFSET_FILE = STATE_DIR / "offset"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_CHAT_ID = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
POLL_TIMEOUT = int(os.environ.get("TELEGRAM_POLL_TIMEOUT", "45"))
POLL_WAIT = int(os.environ.get("TELEGRAM_POLL_WAIT", "2"))
MAX_MSG_LEN = 3900

if not TELEGRAM_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is required")
if not ALLOWED_CHAT_ID:
    raise SystemExit("TELEGRAM_ALLOWED_CHAT_ID is required")

API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


_log_fh = None
running = True


def setup_logging():
    global _log_fh
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _log_fh = open(LOG_FILE, "a", encoding="utf-8")


def log(msg: str, level: str = "INFO"):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} [{level}] {msg}"
    print(line, flush=True)
    if _log_fh:
        _log_fh.write(line + "\n")
        _log_fh.flush()


def tg_get(method: str, params: dict):
    url = f"{API_BASE}/{method}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=70) as resp:
        return json.loads(resp.read())


def tg_post(method: str, body: dict):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/{method}",
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def send_message(chat_id: str, text: str):
    if len(text) > MAX_MSG_LEN:
        text = text[: MAX_MSG_LEN - 3] + "..."
    try:
        tg_post("sendMessage", {"chat_id": chat_id, "text": text})
    except Exception as e:
        log(f"sendMessage failed: {e}", "WARN")


def load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def save_offset(offset: int):
    tmp = OFFSET_FILE.with_suffix(".tmp")
    tmp.write_text(str(offset), encoding="utf-8")
    tmp.replace(OFFSET_FILE)


def load_recover_module():
    spec = importlib.util.spec_from_file_location("slskd_recover", RECOVER_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load module at {RECOVER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_album_query(text: str):
    text = text.strip()
    if not text:
        return None, None, "Empty message. Use: Artist - Album"

    if text.startswith("/"):
        return None, None, None

    if " - " not in text:
        return None, None, "Use format: Artist - Album"

    artist, album = text.split(" - ", 1)
    artist = artist.strip()
    album = album.strip()
    if not artist or not album:
        return None, None, "Use format: Artist - Album"
    return artist, album, None


def process_query(rec, artist: str, album: str):
    label = f"{artist} - {album}"
    query = f"{artist} {album}".strip()
    if len(query) > 60:
        query = query[:60].rsplit(" ", 1)[0]

    pending = rec.pending_download_count()
    if pending >= rec.MAX_PENDING_DL:
        return (
            False,
            f"Queue is busy ({pending}/{rec.MAX_PENDING_DL}). Try again in a few minutes.",
        )

    n_existing = rec.count_existing_tracks(artist, album)
    responses = rec.slskd_search(query)
    if responses is None:
        return False, f"Search error for: {label}"
    if not responses:
        return False, f"No results found for: {label}"

    best = rec.find_best_folder(responses)
    if best is None:
        return False, (
            f"Found {len(responses)} response(s) for {label}, but none passed quality/speed filters."
        )

    if n_existing > 0 and n_existing >= best.file_count:
        return False, (
            f"Skipped {label}: library already has {n_existing} track(s), "
            f"best result has {best.file_count}."
        )

    ok = rec.queue_download(best)
    if not ok:
        return False, f"Failed to queue download for: {label}"

    speed_mb = best.upload_speed / 1_000_000
    msg = (
        f"Queued: {label}\n"
        f"User: {best.username}\n"
        f"Format: {best.fmt.upper()}\n"
        f"Files: {best.file_count}\n"
        f"Speed: {speed_mb:.1f} MB/s\n"
        f"Score: {best.score}"
    )
    if n_existing:
        msg += f"\nLibrary already had {n_existing} track(s) (merge mode)."
    return True, msg


def handle_message(rec, update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat = msg.get("chat", {})
    chat_id = str(chat.get("id", ""))
    if not chat_id:
        return

    if chat_id != ALLOWED_CHAT_ID:
        log(f"Rejected message from unauthorized chat_id={chat_id}", "WARN")
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return

    if text in ("/start", "/help"):
        send_message(
            chat_id,
            "Send album requests as:\nArtist - Album\n\nExample:\nMassive Attack - Mezzanine\n\nCommands:\n/scan — trigger beets import now",
        )
        return

    if text == "/scan":
        result = subprocess.run(
            ["systemctl", "is-active", "beets-import.service"],
            capture_output=True, text=True,
        )
        if result.stdout.strip() == "active":
            send_message(chat_id, "A beets import scan is already in progress.")
        else:
            subprocess.Popen(
                ["systemctl", "start", "beets-import.service"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
            send_message(chat_id, "Beets import scan triggered.")
            log("Manual scan triggered via Telegram")
        return

    artist, album, err = parse_album_query(text)
    if err:
        send_message(chat_id, err)
        return
    if artist is None:
        return

    send_message(chat_id, f"Searching and scoring: {artist} - {album}")
    try:
        ok, result = process_query(rec, artist, album)
        send_message(chat_id, result)
        log(f"Request '{artist} - {album}' -> {'queued' if ok else 'not queued'}")
    except Exception as e:
        log(f"Unhandled processing error: {e}\n{traceback.format_exc()}", "ERROR")
        send_message(chat_id, "Unexpected error while processing request. Check server logs.")


def handle_signal(signum, _frame):
    global running
    running = False
    log(f"Received signal {signum}, shutting down")


def main():
    setup_logging()
    rec = load_recover_module()
    log("slskd-telegram-bot started")

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    offset = load_offset()
    while running:
        try:
            data = tg_get(
                "getUpdates",
                {
                    "timeout": POLL_TIMEOUT,
                    "offset": offset,
                    "allowed_updates": json.dumps(["message", "edited_message"]),
                },
            )
            if not data.get("ok"):
                log(f"Telegram API not ok: {data}", "WARN")
                time.sleep(POLL_WAIT)
                continue

            for upd in data.get("result", []):
                upd_id = upd.get("update_id")
                if isinstance(upd_id, int):
                    offset = max(offset, upd_id + 1)
                    save_offset(offset)
                handle_message(rec, upd)

        except urllib.error.HTTPError as e:
            log(f"Telegram HTTPError {e.code}: {e.read().decode(errors='replace')}", "WARN")
            time.sleep(POLL_WAIT)
        except Exception as e:
            log(f"Polling loop error: {e}", "WARN")
            time.sleep(POLL_WAIT)

    log("slskd-telegram-bot stopped")


if __name__ == "__main__":
    main()
