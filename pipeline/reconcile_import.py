#!/usr/bin/env python3
"""
reconcile_import.py — scheduled consumer side of the pipeline.

The rebuild automated the PRODUCER side (wishlist/recover/... queue downloads
through the slskd ledger) but reconcile — the sole library writer — only ever
ran by hand. This wraps it for unattended use:

    1. poll the slskd in-flight ledger (slskdq.poll) so rows reach terminal
       state on their own — without this, queued/downloading rows outlive their
       transfers forever and only a manual `slskdq --poll` settles them;
    2. compute the BUSY set (slskdq.busy_local_dirs): inbox folders slskd is
       still downloading into. These are shielded from the sweep — slskd moves
       files in one at a time from its incomplete dir, so a slow transfer's
       folder looks mtime-settled between files and would be sliced into park
       fragments (that happened: 2026-07-02, one album parked 4×). If the
       transfers API is unreachable the busy set is UNKNOWN, and this run's
       sweep is skipped entirely — unknown is not empty;
    3. run `reconcile --inbox --execute --min-age-min N --skip-dir ...` so
       settled downloads in INBOX_DIR flow into the beets library through the
       one gate (NEW / UPGRADE / DUPLICATE-discard / PARK-for-review — never a
       silent dup, never a hard delete);
    4. if anything actually landed in the library (NEW or UPGRADE), trigger a
       Plex Music-section refresh so it shows up without waiting for Plex's own
       scan;
    5. surface parks (things that need human eyes) as a notification WITHOUT
       failing the systemd unit — a park is the gate working, not an error.

DRY-RUN-by-default still lives in reconcile.py; this entrypoint always runs
--execute (that's its whole job) and is meant to be driven by a timer.
"""
from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

from . import config as cfg
from . import db as pipeline_db
from . import reconcile

LOG_FILE = str(cfg.LOG_DIR / "reconcile-import.log")

_log_fh = None


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


PLEX_REFRESH_ATTEMPTS = 4
PLEX_REFRESH_BACKOFF  = (5, 15, 30)   # seconds between attempts 1→2, 2→3, 3→4


def _plex_refresh() -> bool:
    """Trigger a full Plex Music-section scan. Reuses the config/section lookup
    already proven in beets_quality_upgrade. Returns True on a 2xx response.

    Retried with backoff: an import lands ~1GB of FLAC and Plex is usually
    mid-scan (FSEvent partial scan) plus analysing loudness when we call. Under
    that load PMS can briefly stop serving HTTP entirely and answer 401 to a
    perfectly valid token — that happened 2026-08-15 16:12:54 on the Light as a
    Feather import, the refresh was skipped, and Plex was left to notice the
    album on its own. It did, but only after publishing a 5-of-6-track album for
    44s. One retry would have closed that window, so don't give up on attempt 1.
    """
    try:
        from . import beets_quality_upgrade as bq
    except Exception as e:  # pragma: no cover - import guard
        log(f"[PLEX] could not import plex helpers: {e}", "WARN")
        return False

    pc = bq._plex_config()
    token = pc.get("token")
    if not token:
        log("[PLEX] no token in beets config — skipping refresh", "WARN")
        return False
    host, port, library = pc["host"], pc["port"], pc["library"]

    last = "unknown"
    for attempt in range(1, PLEX_REFRESH_ATTEMPTS + 1):
        try:
            section_id = bq._plex_section_id(host, port, token, library)
            if section_id is None:
                # Transient too: the section lookup is the call that 401s when
                # PMS is wedged, so treat it as retryable rather than terminal.
                raise RuntimeError(f"could not resolve section '{library}'")
            url = (f"http://{host}:{port}/library/sections/{section_id}"
                   f"/refresh?X-Plex-Token={token}")
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                status = resp.status
            if 200 <= status < 300:
                note = "" if attempt == 1 else f" (attempt {attempt})"
                log(f"[PLEX] refreshed section {section_id} ('{library}') — "
                    f"HTTP {status}{note}")
                return True
            last = f"HTTP {status}"
        except Exception as e:
            last = str(e)

        if attempt < PLEX_REFRESH_ATTEMPTS:
            delay = PLEX_REFRESH_BACKOFF[attempt - 1]
            log(f"[PLEX] refresh attempt {attempt}/{PLEX_REFRESH_ATTEMPTS} failed "
                f"({last}) — retrying in {delay}s", "WARN")
            time.sleep(delay)

    log(f"[PLEX] refresh failed after {PLEX_REFRESH_ATTEMPTS} attempts "
        f"({last}) — Plex may show this import incomplete until its own scan "
        f"catches up", "WARN")
    return False


def _ledger_poll() -> int:
    """Advance live slskd-ledger rows to terminal state (slskdq.poll). Best
    effort by design: in-library-at-admit is authoritative, so a missed poll
    can't cause a dup — an slskd API hiccup here must never fail the import
    run. Returns the number of transitions (0 on error)."""
    try:
        from . import slskdq
        changes = slskdq.poll(execute=True)
    except Exception as e:
        log(f"[LEDGER] poll failed (non-fatal): {e}", "WARN")
        return 0
    for rowid, identity_key, old, new in changes:
        log(f"[LEDGER] #{rowid} {old} -> {new}  {identity_key}")
    if not changes:
        log("[LEDGER] poll: no transitions")
    return len(changes)


def _busy_dirs():
    """Inbox folder names slskd may still be writing into (see module doc §2).
    Returns a set of lowercased basenames, or None when the transfers API is
    unreachable — the caller must then skip the sweep, not treat it as empty."""
    try:
        from . import slskdq
        return slskdq.busy_local_dirs()
    except Exception as e:
        log(f"[BUSY] could not determine active downloads: {e}", "WARN")
        return None


def _prune_empty_dirs(inbox: Path, min_age_min: int, busy=()) -> int:
    """Remove inbox subdirectories that contain NO files at all and have been
    settled at least min_age_min. A successful import moves an album's files out
    (beets move:yes) and leaves the now-empty source dir behind; without this it
    would re-PARK as 'no-audio-files' on every run (notification noise). Only
    truly empty trees are removed, so this can never lose audio. Dirs in `busy`
    (active downloads) are left alone — slskd may be about to move the first
    file in."""
    if not inbox.is_dir():
        return 0
    cutoff = time.time() - min_age_min * 60
    removed = 0
    for child in sorted(inbox.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
            continue
        if child.name.lower() in busy:
            continue
        files = [p for p in child.rglob("*") if p.is_file()]
        if files:
            continue
        try:
            if child.stat().st_mtime > cutoff:
                continue  # too fresh — may be a dir slskd just created
        except OSError:
            continue
        try:
            import shutil
            shutil.rmtree(child)
            log(f"[CLEANUP] removed empty inbox dir: {child.name}")
            removed += 1
        except OSError as e:
            log(f"[CLEANUP] could not remove {child.name}: {e}", "WARN")
    return removed


def _read_summary(run_id: str) -> dict:
    plan = Path(str(cfg.LOG_DIR)) / "reconcile" / run_id / "plan.json"
    try:
        return json.loads(plan.read_text(encoding="utf-8")).get("summary", {})
    except Exception as e:
        log(f"[WARN] could not read plan summary for {run_id}: {e}", "WARN")
        return {}


def _read_landed(run_id: str) -> list[dict]:
    """Per-album detail for the albums this run actually put in the library.

    Reports both the inbox folder name (what was asked for) and the album tag
    beets filed it under. Those diverge on `asis` imports, where the uploader's
    tags win because autotag found no confident MB match — e.g. Maiden Voyage
    arriving tagged as the 'Blue Note 75' box set. Surfacing both means a
    mis-file is visible in the notification instead of being found weeks later.
    """
    plan = Path(str(cfg.LOG_DIR)) / "reconcile" / run_id / "plan.json"
    out: list[dict] = []
    if not plan.exists():
        # _read_summary already warned about the same missing file — the album
        # detail is a nice-to-have on top of the counts, so stay quiet here.
        return out
    try:
        data = json.loads(plan.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"[WARN] could not read plan candidates for {run_id}: {e}", "WARN")
        return out

    for entry in data.get("candidates", []):
        if entry.get("route") not in ("NEW", "UPGRADE"):
            continue
        cand = entry.get("candidate", {}) or {}
        folder = Path(str(cand.get("path", ""))).name
        artist = (cand.get("scanned_albumartist") or "").strip()
        album  = (cand.get("scanned_album") or "").strip()
        out.append({
            "route":  entry.get("route"),
            "folder": folder,
            "artist": artist,
            "album":  album,
            "year":   cand.get("scanned_year"),
            "tracks": cand.get("n_audio_files"),
        })
    return out


def main(argv=None) -> int:
    setup_logging()
    min_age = cfg.RECONCILE_IMPORT_MIN_AGE_MIN
    run_id = "import-" + time.strftime("%Y%m%d-%H%M%S")

    log("===== reconcile-import start =====")
    log(f"inbox={cfg.INBOX_DIR} min_age_min={min_age} run_id={run_id}")

    _ledger_poll()

    busy = _busy_dirs()
    if busy is None:
        log("[BUSY] slskd transfers unknown — skipping this run's sweep "
            "(unknown ≠ empty; next timer fire retries)", "WARN")
        log("===== reconcile-import done (skipped) =====")
        return 0
    if busy:
        log(f"[BUSY] shielding {len(busy)} active download dir(s): {sorted(busy)}")

    pruned = _prune_empty_dirs(Path(str(cfg.INBOX_DIR)), min_age, busy)
    if pruned:
        log(f"[CLEANUP] pruned {pruned} empty inbox dir(s)")

    argv = ["--inbox", "--execute", "--min-age-min", str(min_age), "--run-id", run_id]
    for name in sorted(busy):
        argv += ["--skip-dir", name]
    try:
        rc = reconcile.main(argv)
    except Exception as e:
        # A real failure (locked DB, precondition, crash) — let the unit fail so
        # it's visible. Parks are NOT routed here; they come back as rc==4 below.
        log(f"[ERROR] reconcile raised: {e}", "ERROR")
        return 1

    summ = _read_summary(run_id)
    new = int(summ.get("NEW", 0))
    upg = int(summ.get("UPGRADE", 0))
    dup = int(summ.get("DUPLICATE", 0))
    park = int(summ.get("PARK", 0))
    log(f"[SUMMARY] NEW={new} UPGRADE={upg} DUPLICATE={dup} PARK={park} (reconcile rc={rc})")

    plex_ok = None
    if new or upg:
        plex_ok = _plex_refresh()
    else:
        log("[PLEX] no library changes — refresh skipped")

    # Surface anything imported or needing review, but never fail the unit on a
    # park (a park is the gate doing its job, not an outage).
    if new or upg or park:
        pipeline_db.push_notification(
            "reconcile_import", run_id,
            new=new, upgrade=upg, duplicate=dup, park=park, run_id=run_id,
            albums=_read_landed(run_id), plex_ok=plex_ok,
        )

    log("===== reconcile-import done =====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
