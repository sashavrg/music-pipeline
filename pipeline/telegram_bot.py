#!/usr/bin/env python3
import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import config as cfg
from . import db as pipeline_db
from . import musicbrainz
from . import recover

LOG_FILE = str(cfg.TELEGRAM_BOT_LOG)

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
# TELEGRAM_ALLOWED_CHAT_ID accepts one id or a comma/space-separated list.
ALLOWED_CHAT_IDS  = {
    c.strip()
    for c in re.split(r"[,\s]+", os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", ""))
    if c.strip()
}
POLL_TIMEOUT      = cfg.TELEGRAM_POLL_TIMEOUT
POLL_WAIT         = cfg.TELEGRAM_POLL_WAIT
OFFSET_FILE       = cfg.BOT_STATE_DIR / "offset"
MAX_MSG_LEN       = 3900

if not TELEGRAM_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN is required")
if not ALLOWED_CHAT_IDS:
    raise SystemExit("TELEGRAM_ALLOWED_CHAT_ID is required")

API_BASE = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

_log_fh = None
running = True


def setup_logging():
    global _log_fh
    _log_fh = cfg.open_log_file(LOG_FILE)


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
        f"{API_BASE}/{method}", data=data, method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def send_message(chat_id: str, text: str) -> bool:
    if len(text) > MAX_MSG_LEN:
        text = text[: MAX_MSG_LEN - 3] + "..."
    try:
        tg_post("sendMessage", {"chat_id": chat_id, "text": text})
        return True
    except Exception as e:
        log(f"sendMessage failed: {e}", "WARN")
        return False


def load_offset() -> int:
    try:
        return int(OFFSET_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return 0


def save_offset(offset: int):
    OFFSET_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = OFFSET_FILE.with_suffix(".tmp")
    tmp.write_text(str(offset), encoding="utf-8")
    tmp.replace(OFFSET_FILE)


# ── Notification rendering ────────────────────────────────────────────────────

def _fmt_ts(ts: str) -> str:
    return f"\n{ts}" if ts else ""


def render_notification(notif: dict) -> tuple[str | None, str | None]:
    """Return (log_msg, telegram_text) for a notification. None = skip."""
    event  = notif.get("event", "")
    folder = notif.get("folder", "?")
    ts     = notif.get("time", "")

    if event == "promoted":
        files = notif.get("files", "?")
        return (
            f"Sent album notification: {folder}",
            f"✅ Album download complete\n{folder}\nFiles: {files}{_fmt_ts(ts)}",
        )

    if event == "dedup_detected":
        lib_tracks = notif.get("lib_tracks", "?")
        artist     = notif.get("artist", "")
        album      = notif.get("album", "")
        label = f"{artist} - {album}" if artist else album
        return (
            f"Sent dedup-detected notification: {folder}",
            f"⚠️ Duplicate detected\n{folder}\n"
            f"Library already has {lib_tracks} track(s) for \"{label}\"\n"
            f"Beets will merge/upgrade automatically.{_fmt_ts(ts)}",
        )

    if event == "escalated":
        reason  = notif.get("reason", "?")
        hold_h  = notif.get("hold_hours", "?")
        return (
            f"Sent escalation notification: {folder}",
            f"⚠️ Incomplete album escalated to quarantine\n{folder}\n"
            f"Held for: {hold_h}h\nReason: {reason}{_fmt_ts(ts)}",
        )

    if event == "incomplete_stalled":
        stalled_h = notif.get("stalled_hours", "?")
        audio     = notif.get("audio_files", "?")
        return (
            f"Sent stalled notification: {folder}",
            f"⏳ Stalled incomplete download\n{folder}\n"
            f"Stalled: {stalled_h}h | Files so far: {audio}\n"
            f"Will auto-requeue at 48h, quarantine at 7d{_fmt_ts(ts)}",
        )

    if event == "incomplete_requeued":
        files     = notif.get("files", "?")
        user      = notif.get("user", "?")
        score     = notif.get("score", "?")
        stalled_h = notif.get("stalled_hours", "?")
        return (
            f"Sent requeue notification: {folder}",
            f"↺ Re-queued stalled download\n{folder}\n"
            f"Was stalled: {stalled_h}h\n"
            f"User: {user} | Files: {files} | Score: {score}{_fmt_ts(ts)}",
        )

    if event == "incomplete_no_results":
        query     = notif.get("query", "?")
        stalled_h = notif.get("stalled_hours", "?")
        return (
            f"Sent no-results notification: {folder}",
            f"❌ No requeue results\n{folder}\n"
            f"Stalled: {stalled_h}h | Query tried: {query}\n"
            f"Will quarantine at 7d{_fmt_ts(ts)}",
        )

    if event == "incomplete_quarantined":
        stalled_h = notif.get("stalled_hours", "?")
        audio     = notif.get("audio_files", "?")
        return (
            f"Sent quarantine notification: {folder}",
            f"\U0001f5d1️ Incomplete download quarantined\n{folder}\n"
            f"Stalled: {stalled_h}h | Files rescued: {audio}{_fmt_ts(ts)}",
        )

    if event == "beets_import_stuck":
        cycles = notif.get("consecutive_cycles", "?")
        return (
            f"Sent beets-import-stuck notification: {folder}",
            f"⚠️ Folder stuck in beets import\n{folder}\n"
            f"Seen {cycles} consecutive 15-min cycles without being absorbed.\n"
            f"Check beets log; may need manual import or quarantine.{_fmt_ts(ts)}",
        )

    if event == "audiobook_routed":
        target = notif.get("target", "?")
        reason = notif.get("reason", "")
        return (
            f"Sent audiobook-routed notification: {folder}",
            f"\U0001f4d6 Audiobook routed\n{folder}\n→ {target}\n"
            f"signals: {reason}{_fmt_ts(ts)}",
        )

    if event == "healthcheck_alert":
        message = notif.get("message", "pipeline health issue detected")
        return (
            "Sent healthcheck alert",
            f"\U0001f6a8 {message}{_fmt_ts(ts)}",
        )

    if event == "fill_queued":
        tracks       = notif.get("tracks", [])
        files        = notif.get("files", "?")
        user         = notif.get("user", "?")
        fmt          = notif.get("fmt", "?")
        score        = notif.get("score", "?")
        missing_total = notif.get("missing_total", "?")
        return (
            f"Sent fill-queued notification: {folder}",
            f"\U0001f9e9 Filling missing tracks\n{folder}\n"
            f"Queuing {files} file(s) for tracks {tracks}\n"
            f"({files} of {missing_total} missing) | {fmt} | {user} | score {score}{_fmt_ts(ts)}",
        )

    if event == "fill_no_results":
        missing = notif.get("missing", [])
        query   = notif.get("query", "?")
        return (
            f"Sent fill-no-results notification: {folder}",
            f"❌ No results for missing tracks\n{folder}\n"
            f"Missing: {missing}\nQuery tried: {query}{_fmt_ts(ts)}",
        )

    if event == "quarantine_requeued":
        user  = notif.get("user", "?")
        fmt   = notif.get("fmt", "?")
        files = notif.get("files", "?")
        score = notif.get("score", "?")
        return (
            f"Sent quarantine-requeued notification: {folder}",
            f"↺ Quarantine re-queued\n{folder}\n"
            f"User: {user} | {fmt} | {files} files | score {score}{_fmt_ts(ts)}",
        )

    if event == "quarantine_cleared":
        lib_tracks = notif.get("lib_tracks", "?")
        return (
            f"Sent quarantine-cleared notification: {folder}",
            f"✅ Quarantine cleared — already in library\n{folder}\n"
            f"Library tracks found: {lib_tracks}{_fmt_ts(ts)}",
        )

    if event == "quarantine_no_results":
        query = notif.get("query", "?")
        return (
            f"Sent quarantine-no-results notification: {folder}",
            f"❌ Quarantine re-queue: no results\n{folder}\n"
            f"Query tried: {query}\nWill retry in 7 days{_fmt_ts(ts)}",
        )

    if event == "wishlist_queued":
        artist = notif.get("artist", "")
        album  = notif.get("album", "")
        user   = notif.get("user", "?")
        fmt    = notif.get("fmt", "?")
        return (
            f"Sent wishlist-queued notification: {artist} - {album}",
            f"\U0001f31f Wishlist item found & queued!\n{artist} - {album}\n"
            f"User: {user} | Format: {fmt}{_fmt_ts(ts)}",
        )

    if event == "wishlist_no_results":
        artist = notif.get("artist", "")
        album  = notif.get("album", "")
        return (
            f"Sent wishlist-no-results: {artist} - {album}",
            None,   # silent — daily no-results are noise; digest covers this
        )

    # Batched events ──────────────────────────────────────────────────────────

    if event == "quarantine_requeued_batch":
        count = notif.get("count", "?")
        items = notif.get("items", [])
        names = "\n".join(f"  • {i.get('folder', '?')}" for i in items[:8])
        extra = f"\n  … and {len(items) - 8} more" if len(items) > 8 else ""
        return (
            f"Sent quarantine-requeued-batch: {count} items",
            f"↺ Quarantine sweep: {count} re-queued\n{names}{extra}",
        )

    if event == "quarantine_cleared_batch":
        count = notif.get("count", "?")
        items = notif.get("items", [])
        names = "\n".join(f"  • {i.get('folder', '?')}" for i in items[:8])
        extra = f"\n  … and {len(items) - 8} more" if len(items) > 8 else ""
        return (
            f"Sent quarantine-cleared-batch: {count} items",
            f"✅ Quarantine sweep: {count} cleared (already in library)\n{names}{extra}",
        )

    if event == "quarantine_no_results_batch":
        count = notif.get("count", "?")
        return (
            f"Sent quarantine-no-results-batch: {count} items",
            f"❌ Quarantine sweep: {count} items — no results found, will retry in 7 days",
        )

    if event == "incomplete_in_library_removed_batch":
        count = notif.get("count", "?")
        return (
            f"Sent in-library-removed-batch: {count} items",
            f"✅ Cleanup: {count} incomplete folders removed (already in library)",
        )

    if event == "quarantine_dead_letter":
        key = notif.get("folder", "")
        n = notif.get("attempts", "?")
        return (
            f"Dead-lettered {key} after {n} fruitless cycles",
            f"\u26b0\ufe0f Quarantine gave up on \u201c{key}\u201d after {n} "
            f"fruitless weekly cycles.\nMoved to unparsed/ \u2014 needs a human "
            f"(bad tags / unfindable release).",
        )

    if event == "weekly_digest":
        new_albums     = notif.get("new_albums", [])
        new_tracks     = notif.get("new_tracks", 0)
        held           = notif.get("held", [])
        events_by_type = notif.get("events_by_type", {})

        lines = [f"\U0001f4c5 Weekly pipeline digest"]
        lines.append(f"\nImports (last 7 days): {len(new_albums)} albums, {new_tracks} tracks")

        if new_albums:
            for alb in new_albums[:8]:
                lines.append(f"  • {alb.get('artist', '')} – {alb.get('album', '')} ({alb.get('tracks', '?')} trk)")
            if len(new_albums) > 8:
                lines.append(f"  … and {len(new_albums) - 8} more")

        promoted  = events_by_type.get("promoted", 0)
        escalated = events_by_type.get("escalated", 0)
        filled    = events_by_type.get("fill_queued", 0)
        if promoted or escalated or filled:
            lines.append(f"\nPipeline activity:")
            if promoted:
                lines.append(f"  Promoted: {promoted}")
            if filled:
                lines.append(f"  Fill attempts: {filled}")
            if escalated:
                lines.append(f"  Escalated to quarantine: {escalated}")

        if held:
            lines.append(f"\nStill held ({len(held)}):")
            for h in held[:5]:
                lines.append(f"  • {h['name']} ({h['age_h']:.0f}h)")
            if len(held) > 5:
                lines.append(f"  … and {len(held) - 5} more")

        parks = notif.get("parks", [])
        parks_total = notif.get("parks_total", len(parks))
        if parks_total:
            lines.append(f"\n🅿️ Parked, waiting on you ({parks_total}):")
            for p in parks[:6]:
                why = f" — {p['reason']}" if p.get("reason") else ""
                lines.append(f"  • {p['name']} ({p['age_days']:.0f}d, {p['n_files']}f){why}")
            if parks_total > 6:
                lines.append(f"  … and {parks_total - 6} more (pipeline-parks for the full list)")

        stuck = notif.get("ledger_stuck", [])
        if stuck:
            lines.append(f"\n⏳ Ledger rows in flight >24h ({len(stuck)}):")
            for r in stuck[:4]:
                lines.append(f"  • {r['artist']} – {r['album']} [{r['state']}] {r['age_h']:.0f}h")

        qfail = notif.get("quarantine_failing", 0)
        if qfail:
            lines.append(f"\n♻️ Quarantine items failing toward dead-letter: {qfail}")

        return (
            f"Sent weekly digest: {len(new_albums)} albums, {new_tracks} tracks",
            "\n".join(lines),
        )

    return None, None


# ── Command handling ──────────────────────────────────────────────────────────


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
    album  = album.strip()
    if not artist or not album:
        return None, None, "Use format: Artist - Album"
    return artist, album, None


def _build_query(artist: str, album: str) -> str:
    """User-facing query format slskd searches against. Truncated to 60 chars
    on a word boundary because slskd's distributed network drops very long
    queries."""
    q = f"{artist} {album}".strip()
    if len(q) > 60:
        q = q[:60].rsplit(" ", 1)[0]
    return q


def _mb_retry_query(artist: str, album: str, original_query: str, profile):
    """
    On a 0-result search, ask MusicBrainz for the canonical (artist, album)
    and build a retry query if it would actually be different. Returns
    (retry_query, canonical_label) or (None, None) if no retry is worth running.

    Skipped for AUDIOBOOK — MB is a music-release DB and would only mislead.
    """
    if profile is recover.AUDIOBOOK:
        return None, None
    try:
        canon = musicbrainz.lookup_canonical_release(artist, album)
    except Exception as e:
        log(f"MB canonical lookup failed for {artist} - {album}: {e}", "WARN")
        return None, None
    if not canon:
        return None, None
    retry_q = _build_query(canon['artist_credit'], canon['title'])
    # No point retrying if MB canonicalises to the same words we already sent.
    if retry_q.lower() == original_query.lower():
        return None, None
    canonical_label = f"{canon['artist_credit']} - {canon['title']}"
    return retry_q, canonical_label


def process_query(rec, artist: str, album: str, profile=None):
    # `rec` is the recover module (kept as a parameter for testability —
    # tests can pass a mock). Module-level `from . import recover` means
    # this is never None at runtime, so no defensive guard.
    if profile is None:
        profile = recover.MUSIC
    label = f"{artist} - {album}"
    query = _build_query(artist, album)

    pending = recover.pending_download_count()
    if pending >= recover.MAX_PENDING_DL:
        return False, f"Queue is busy ({pending}/{recover.MAX_PENDING_DL}). Try again in a few minutes."

    # Audiobooks live outside the music library, so the dedup check that
    # compares against beets is meaningless for them.
    n_existing = 0 if profile is recover.AUDIOBOOK else recover.count_existing_tracks(artist, album)
    responses  = recover.slskd_search(query)
    if responses is None:
        return False, f"Search error for: {label}"

    # MusicBrainz retry: the bot's "{artist} {album}" query drops 0 results
    # on collab albums (e.g. user types 'Marvin Gaye - United' but Soulseek
    # only finds files credited to 'Marvin Gaye & Tammi Terrell'). Resolve
    # the canonical artist-credit + title via MB and try once more.
    retry_label = None
    if not responses:
        retry_q, retry_label = _mb_retry_query(artist, album, query, profile)
        if retry_q:
            log(f"MB retry: '{query}' -> '{retry_q}' ({retry_label})")
            responses = recover.slskd_search(retry_q)
            if responses is None:
                return False, f"Search error for: {label}"
            if responses:
                # For downstream dedup, use the canonical artist/title once we
                # actually matched on them — the canonical label is what gets
                # imported into beets.
                if retry_label:
                    artist, album = retry_label.split(" - ", 1)
                    label = retry_label
                    n_existing = 0 if profile is recover.AUDIOBOOK else recover.count_existing_tracks(artist, album)

    if not responses:
        hint = f"\n(also tried MusicBrainz '{retry_label}' — no results)" if retry_label else ""
        return False, f"No results found for: {label}{hint}"

    best = recover.find_best_folder(responses, artist=artist, album=album, profile=profile)
    if best is None:
        return False, (
            f"Found {len(responses)} response(s) for {label}, but none passed "
            f"quality/speed filters ({profile.name} profile)."
        )

    if n_existing > 0 and n_existing >= best.file_count:
        return False, (
            f"Skipped {label}: library already has {n_existing} track(s), "
            f"best result has {best.file_count}."
        )

    ok = recover.queue_download(best)
    if not ok:
        return False, f"Failed to queue download for: {label}"

    speed_mb = best.upload_speed / 1_000_000
    profile_tag = f" [{profile.name}]" if profile is not recover.MUSIC else ""
    msg = (
        f"Queued{profile_tag}: {label}\n"
        f"User: {best.username}\n"
        f"Format: {best.fmt.upper()}\n"
        f"Files: {best.file_count}\n"
        f"Speed: {speed_mb:.1f} MB/s\n"
        f"Score: {best.score}"
    )
    if retry_label:
        msg += f"\n(Matched via MusicBrainz expansion.)"
    if n_existing:
        msg += f"\nLibrary already had {n_existing} track(s) (merge mode)."
    return True, msg


def _time_ago(ts: float | None) -> str:
    if not ts:
        return "never"
    delta = time.time() - ts
    if delta < 3600:
        return f"{int(delta/60)}m ago"
    if delta < 86400:
        return f"{int(delta/3600)}h ago"
    return f"{int(delta/86400)}d ago"


def handle_wishlist_command(chat_id: str, args: str, kind: str = 'music'):
    """Handle /wish [remove N | Artist - Album]. `kind` is 'music' or 'audiobook'."""
    args = args.strip()
    cmd_label = '/bookwish' if kind == 'audiobook' else '/wish'

    # /wish (no args) → list (always shows everything, regardless of kind)
    if not args or args in ("list", "ls"):
        items = pipeline_db.get_wishlist_pending()
        if not items:
            send_message(chat_id, f"\U0001f4cb Wishlist is empty.\n\nAdd items with: {cmd_label} Artist - Album")
            return

        lines = [f"\U0001f4cb Wishlist ({len(items)} items):"]
        for item in items:
            added   = _time_ago(item['added_at'])
            queued  = f", queued {_time_ago(item['last_queued'])}" if item.get('last_queued') else ""
            tried   = f", tried {_time_ago(item['last_attempt'])}" if item.get('last_attempt') else ""
            status  = queued or tried or ", not yet searched"
            tag = " \U0001f4d6" if item.get('kind') == 'audiobook' else ""
            lines.append(f"  {item['id']}.{tag} {item['artist']} - {item['album']} ({added}{status})")
        send_message(chat_id, "\n".join(lines))
        return

    # /wish remove N (works the same regardless of kind)
    m = re.match(r'^(?:remove|rm|done|del)\s+(\d+)$', args, re.IGNORECASE)
    if m:
        wid = int(m.group(1))
        ok  = pipeline_db.remove_wishlist(wid)
        send_message(chat_id, f"Removed wishlist item #{wid}." if ok
                     else f"No wishlist item with id {wid}.")
        return

    if " - " not in args:
        send_message(chat_id, f"Use: {cmd_label} Artist - Album\nOr: /wish remove N")
        return

    artist, album = args.split(" - ", 1)
    artist = artist.strip()
    album  = album.strip()
    if not artist or not album:
        send_message(chat_id, f"Use: {cmd_label} Artist - Album")
        return

    wid, is_new = pipeline_db.add_wishlist(artist, album, kind=kind)
    if is_new:
        tag = "\U0001f4d6 audiobook " if kind == 'audiobook' else ""
        send_message(chat_id,
                     f"\U0001f31f Added {tag}to wishlist: {artist} - {album} (id={wid})\n"
                     f"I'll search for it daily.")
        log(f"Wishlist add #{wid} ({kind}): {artist} - {album}")
    else:
        send_message(chat_id, f"Already on wishlist: {artist} - {album} (id={wid})")


def handle_message(update: dict):
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return

    chat    = msg.get("chat", {})
    chat_id = str(chat.get("id", ""))
    if not chat_id:
        return

    if chat_id not in ALLOWED_CHAT_IDS:
        log(f"Rejected message from unauthorized chat_id={chat_id}", "WARN")
        return

    text = (msg.get("text") or "").strip()
    if not text:
        return

    if text in ("/start", "/help"):
        send_message(
            chat_id,
            "Music pipeline bot\n\n"
            "Queue an album:\n  Artist - Album\n\n"
            "Commands:\n"
            "  /scan — trigger beets import now\n"
            "  /wish Artist - Album — add to wishlist\n"
            "  /wish — show wishlist\n"
            "  /wish remove N — remove wishlist item\n"
            "  /book Author - Title — queue an audiobook (relaxed quality)\n"
            "  /bookwish Author - Title — wishlist an audiobook\n"
            "  /status — pipeline status summary",
        )
        return

    if text == "/scan":
        # RETIRED 2026-06-14: /scan no longer starts the old ungated beets-import.service
        # (systemctl disable did NOT block a manual start, so this was a reachable
        # ungated writer). Imports now go through reconcile.py — the sole library writer
        # / identity gate — which is dry-run by default and never writes from a chat
        # command. /scan now just reports what's waiting in the inbox.
        try:
            inbox = str(cfg.INBOX_DIR)
            folders = [d for d in os.listdir(inbox)
                       if os.path.isdir(os.path.join(inbox, d)) and not d.startswith(("_", "."))]
            n = str(len(folders))
        except Exception:
            n = "unknown"
        send_message(
            chat_id,
            "Auto-import via /scan is retired — imports now go through the reconcile "
            "gate (reconcile.py), which is dry-run by default and never writes from a "
            "chat command.\n"
            f"\U0001f4e5 Inbox folders awaiting reconcile: {n}\n"
            "Dry-run plan:  python3 -m pipeline.reconcile --inbox\n"
            "Execute (gated):  add --execute",
        )
        log("/scan reported inbox status (ungated beets-import path retired)")
        return

    if text == "/status":
        held   = pipeline_db.get_held_folders()
        wish   = pipeline_db.get_wishlist_pending()
        lines  = ["\U0001f4ca Pipeline status"]
        if held:
            lines.append(f"\n\U0001f551 Held folders ({len(held)}):")
            for name, entry in sorted(held.items()):
                age_h = (time.time() - entry['first_seen']) / 3600
                lines.append(f"  • {name} ({age_h:.0f}h)")
        else:
            lines.append("✅ No folders held")
        if wish:
            lines.append(f"\n\U0001f31f Wishlist: {len(wish)} pending item(s)")
        send_message(chat_id, "\n".join(lines))
        return

    if text.startswith("/bookwish"):
        handle_wishlist_command(chat_id, text[len("/bookwish"):].strip(),
                                kind='audiobook')
        return

    if text.startswith("/wish"):
        handle_wishlist_command(chat_id, text[5:].strip())
        return

    if text.startswith("/book"):
        body = text[len("/book"):].strip()
        artist, album, err = parse_album_query(body)
        if err:
            send_message(chat_id, err)
            return
        if artist is None:
            return
        send_message(chat_id, f"\U0001f4d6 Searching audiobook (relaxed quality): {artist} - {album}")
        try:
            ok, result = process_query(recover, artist, album, profile=recover.AUDIOBOOK)
            send_message(chat_id, result)
            outcome = "queued" if ok else "not queued"
            first_line = result.splitlines()[0] if result else ""
            log(f"Audiobook request '{artist} - {album}' -> {outcome}: {first_line}")
        except Exception as e:
            log(f"Unhandled audiobook processing error: {e}\n{traceback.format_exc()}", "ERROR")
            send_message(chat_id, "Unexpected error while processing request. Check server logs.")
        return

    if text.startswith("/"):
        return  # unknown command — ignore

    artist, album, err = parse_album_query(text)
    if err:
        send_message(chat_id, err)
        return
    if artist is None:
        return

    send_message(chat_id, f"Searching and scoring: {artist} - {album}")
    try:
        ok, result = process_query(recover, artist, album)
        send_message(chat_id, result)
        outcome = "queued" if ok else "not queued"
        first_line = result.splitlines()[0] if result else ""
        log(f"Request '{artist} - {album}' -> {outcome}: {first_line}")
    except Exception as e:
        log(f"Unhandled processing error: {e}\n{traceback.format_exc()}", "ERROR")
        send_message(chat_id, "Unexpected error while processing request. Check server logs.")


def handle_signal(signum, _frame):
    global running
    running = False
    log(f"Received signal {signum}, shutting down")


_INSTANCE_LOCK = None


def acquire_instance_lock():
    """Single-instance guard. Prevents two overlapping bots from double-draining notifications."""
    global _INSTANCE_LOCK
    lock_path = str(cfg.BOT_STATE_DIR / "bot.lock")
    Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit(f"Another slskd-telegram-bot instance holds {lock_path}; refusing to start")
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    _INSTANCE_LOCK = fh  # keep open for process lifetime


def main():
    setup_logging()
    acquire_instance_lock()
    pipeline_db.init_db()

    # recover is imported at module top — if that failed we wouldn't be here.
    log("slskd-telegram-bot started")

    signal.signal(signal.SIGINT,  handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    offset = load_offset()
    while running:
        try:
            data = tg_get(
                "getUpdates",
                {
                    "timeout": POLL_TIMEOUT,
                    "offset":  offset,
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
                handle_message(upd)

            # ── Drain and deliver notifications ───────────────────────────────
            # Two-phase: claim (does NOT mark delivered), send, then mark delivered.
            # On crash between claim and mark, rows stay pending and are retried.
            for notif, ids in pipeline_db.claim_notifications():
                log_msg, tg_text = render_notification(notif)
                sent = True
                if tg_text:
                    # Fan out to every allowed chat. Marked delivered only if all
                    # succeed; a partial failure retries the whole batch (may
                    # re-deliver to chats that already got it — acceptable at
                    # this volume).
                    sent = all(
                        send_message(cid, tg_text) for cid in sorted(ALLOWED_CHAT_IDS)
                    )
                if sent:
                    pipeline_db.mark_delivered(ids)
                    if log_msg:
                        log(log_msg)
                else:
                    log(f"Notification send failed; will retry: {notif.get('event')}", "WARN")

        except urllib.error.HTTPError as e:
            log(f"Telegram HTTPError {e.code}: {e.read().decode(errors='replace')}", "WARN")
            time.sleep(POLL_WAIT)
        except Exception as e:
            log(f"Polling loop error: {e}", "WARN")
            time.sleep(POLL_WAIT)

    log("slskd-telegram-bot stopped")


if __name__ == "__main__":
    main()
