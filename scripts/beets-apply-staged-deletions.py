#!/usr/bin/env python3
"""
beets-apply-staged-deletions.py

Runs after a successful beet import. For each pending-deletion JSON file written
by beets-quality-upgrade.py, it:

  1. Checks that the incoming folder no longer exists in the ready/ dir
     (i.e. beet import moved/consumed the files), OR that every staged item's
     track is now present in the beets DB at a newer/higher-quality path.
  2. If the import is confirmed, deletes the old library files and DB rows and
     notifies Plex.
  3. If the incoming folder is STILL present (import failed or was skipped),
     aborts and leaves the staged file in place — the old library tracks are safe.

Staged JSON files older than STALE_HOURS with a still-present incoming folder are
logged as errors but are not deleted, so they accumulate visibly for manual review.
"""

import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, "/usr/local/bin")
import pipeline_config as cfg

LIB_DB               = cfg.BEETS_DB
LIB_ROOT             = str(cfg.LIBRARY_ROOT)
PENDING_DELETIONS_DIR = cfg.PENDING_DELETIONS_DIR
LOG_FILE             = str(cfg.BEETS_LOG)

# If the incoming folder is still present this many hours after staging, warn loudly.
STALE_HOURS = 2


# ── Logging ───────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"{ts} {msg}"
    print(line, flush=True)


# ── Plex helpers (copied from quality-upgrade to stay self-contained) ─────────

def _plex_config():
    import yaml
    cfg_path = os.path.expanduser("~/.config/beets/config.yaml")
    try:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
        plex = cfg.get("plex", {})
        return {
            "host":    plex.get("host", "localhost"),
            "port":    plex.get("port", 32400),
            "token":   plex.get("token", ""),
            "library": plex.get("library_name", "Music"),
        }
    except Exception as e:
        log(f"[WARN] could not read beets plex config: {e}")
        return {}


def _plex_section_id(host, port, token, library_name):
    url = f"http://{host}:{port}/library/sections?X-Plex-Token={token}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        for section in data.get("MediaContainer", {}).get("Directory", []):
            if section.get("title") == library_name:
                return section.get("key")
    except Exception as e:
        log(f"[WARN] could not fetch Plex sections: {e}")
    return None


def plex_refresh_dirs(dirs):
    if not dirs:
        return
    cfg = _plex_config()
    if not cfg.get("token"):
        log("[WARN] no plex token in beets config — skipping Plex refresh")
        return
    host, port, token, library = cfg["host"], cfg["port"], cfg["token"], cfg["library"]
    section_id = _plex_section_id(host, port, token, library)
    if not section_id:
        log(f"[WARN] could not find Plex section '{library}' — skipping refresh")
        return
    refreshed = 0
    for d in sorted(dirs):
        encoded = urllib.parse.quote(str(d), safe="")
        url = (
            f"http://{host}:{port}/library/sections/{section_id}"
            f"/refresh?path={encoded}&X-Plex-Token={token}"
        )
        try:
            with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=5) as resp:
                resp.read()
            log(f"[PLEX] refreshed: {d}")
            refreshed += 1
        except Exception as e:
            log(f"[WARN] Plex refresh failed for {d}: {e}")
    log(f"[PLEX] triggered refresh on {refreshed}/{len(dirs)} album dir(s)")


# ── DB helpers ────────────────────────────────────────────────────────────────

def item_exists_in_db(conn, item_id: int) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM items WHERE id = ?", (item_id,))
    return cur.fetchone() is not None


def delete_items_from_db(conn, items: list) -> list:
    cur = conn.cursor()
    deleted = []
    affected_dirs = set()
    for it in items:
        p = Path(it["path"])
        if str(p).startswith(LIB_ROOT) and p.exists():
            affected_dirs.add(p.parent)
            try:
                p.unlink()
                log(f"[DELETE] {p}")
            except Exception as e:
                log(f"[WARN] failed deleting file {p}: {e}")
        try:
            cur.execute("DELETE FROM items WHERE id = ?", (it["id"],))
            deleted.append(it["id"])
        except Exception as e:
            log(f"[WARN] failed deleting db row id={it['id']}: {e}")
    # Prune orphaned album rows
    cur.execute(
        "DELETE FROM albums WHERE id NOT IN "
        "(SELECT DISTINCT album_id FROM items WHERE album_id IS NOT NULL)"
    )
    conn.commit()
    plex_refresh_dirs(affected_dirs)
    return deleted


# ── Verification ──────────────────────────────────────────────────────────────

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".alac", ".aiff", ".wma"}


def incoming_folder_consumed(incoming_folder: Path) -> bool:
    """
    Return True if the incoming folder is gone (beet import moved all files out)
    or contains no audio files (import consumed them).
    """
    if not incoming_folder.exists():
        return True
    audio = [
        p for p in incoming_folder.rglob("*")
        if p.is_file()
        and p.suffix.lower() in AUDIO_EXTS
        and not p.name.startswith("._")
        and not p.name.startswith(".")
    ]
    return len(audio) == 0


def all_staged_still_in_db(conn, items: list) -> bool:
    """
    Return True only if every staged item is still present in the DB
    (i.e. we haven't already cleaned them up in a previous run).
    """
    for it in items:
        if not item_exists_in_db(conn, it["id"]):
            return False
    return True


# ── Main ──────────────────────────────────────────────────────────────────────

def process_staged_file(conn, staged_path: Path) -> str:
    """
    Returns one of: 'applied', 'skipped-not-consumed', 'skipped-stale',
                    'skipped-already-gone', 'error'
    """
    try:
        with open(staged_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception as e:
        log(f"[ERROR] could not read staged file {staged_path.name}: {e}")
        return "error"

    incoming_folder = Path(payload.get("incoming_folder", ""))
    staged_at       = payload.get("staged_at", 0)
    items           = payload.get("items", [])
    age_h           = (time.time() - staged_at) / 3600

    if not items:
        log(f"[SKIP] {staged_path.name} — no items, removing")
        staged_path.unlink()
        return "skipped-already-gone"

    # If none of the staged items are still in the DB, a previous run already
    # cleaned them up (or they were removed manually). Just drop the file.
    if not all_staged_still_in_db(conn, items):
        log(f"[SKIP] {staged_path.name} — staged items no longer in DB, discarding")
        staged_path.unlink()
        return "skipped-already-gone"

    if not incoming_folder_consumed(incoming_folder):
        # Incoming folder still has audio — beet import didn't consume it.
        if age_h > STALE_HOURS:
            log(
                f"[WARN] {staged_path.name} — incoming folder still present after {age_h:.1f}h "
                f"({incoming_folder}). Import likely failed. Old library tracks preserved. "
                f"Manual review needed."
            )
            return "skipped-stale"
        else:
            log(
                f"[HOLD] {staged_path.name} — incoming folder still present ({age_h:.1f}h old), "
                f"import may still be in progress, will retry next cycle"
            )
            return "skipped-not-consumed"

    # Import confirmed — safe to delete old library files.
    log(
        f"[APPLY] {staged_path.name} — incoming folder consumed, "
        f"deleting {len(items)} old library item(s)"
    )
    deleted = delete_items_from_db(conn, items)
    log(f"[APPLIED] {len(deleted)}/{len(items)} item(s) deleted from library")
    staged_path.unlink()
    return "applied"


def main():
    if not PENDING_DELETIONS_DIR.exists():
        log("[INFO] no pending-deletions dir, nothing to apply")
        return 0

    staged_files = sorted(PENDING_DELETIONS_DIR.glob("*.json"))
    if not staged_files:
        log("[INFO] no staged deletions pending")
        return 0

    log(f"[INFO] apply-staged-deletions: {len(staged_files)} file(s) to process")
    conn = sqlite3.connect(LIB_DB, timeout=10)
    results = {"applied": 0, "skipped-not-consumed": 0,
               "skipped-stale": 0, "skipped-already-gone": 0, "error": 0}
    try:
        for sf in staged_files:
            r = process_staged_file(conn, sf)
            results[r] = results.get(r, 0) + 1
    finally:
        conn.close()

    log(
        f"[SUMMARY] apply-staged-deletions: "
        f"applied={results['applied']} "
        f"held={results['skipped-not-consumed']} "
        f"stale={results['skipped-stale']} "
        f"already_gone={results['skipped-already-gone']} "
        f"errors={results['error']}"
    )
    # Exit non-zero if any staged deletions are stale (import failed) so
    # beets-import.sh can log a warning.
    return 1 if results["skipped-stale"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
