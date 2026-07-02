#!/usr/bin/env python3
"""
reconcile_import.py — scheduled consumer side of the pipeline.

The rebuild automated the PRODUCER side (wishlist/recover/... queue downloads
through the slskd ledger) but reconcile — the sole library writer — only ever
ran by hand. This wraps it for unattended use:

    1. poll the slskd in-flight ledger (slskdq.poll) so rows reach terminal
       state on their own — without this, queued/downloading rows outlive their
       transfers forever and only a manual `slskdq --poll` settles them;
    2. run `reconcile --inbox --execute --min-age-min N` so settled downloads
       in INBOX_DIR flow into the beets library through the one gate (NEW /
       UPGRADE / DUPLICATE-discard / PARK-for-review — never a silent dup, never
       a hard delete);
    3. if anything actually landed in the library (NEW or UPGRADE), trigger a
       Plex Music-section refresh so it shows up without waiting for Plex's own
       scan;
    4. surface parks (things that need human eyes) as a notification WITHOUT
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


def _plex_refresh() -> bool:
    """Trigger a full Plex Music-section scan. Reuses the config/section lookup
    already proven in beets_quality_upgrade. Returns True on a 2xx response."""
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
    section_id = bq._plex_section_id(host, port, token, library)
    if section_id is None:
        log(f"[PLEX] could not resolve section '{library}' — skipping refresh", "WARN")
        return False
    url = f"http://{host}:{port}/library/sections/{section_id}/refresh?X-Plex-Token={token}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            ok = 200 <= resp.status < 300
        log(f"[PLEX] refreshed section {section_id} ('{library}') — HTTP {resp.status}")
        return ok
    except Exception as e:
        log(f"[PLEX] refresh failed: {e}", "WARN")
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


def _prune_empty_dirs(inbox: Path, min_age_min: int) -> int:
    """Remove inbox subdirectories that contain NO files at all and have been
    settled at least min_age_min. A successful import moves an album's files out
    (beets move:yes) and leaves the now-empty source dir behind; without this it
    would re-PARK as 'no-audio-files' on every run (notification noise). Only
    truly empty trees are removed, so this can never lose audio."""
    if not inbox.is_dir():
        return 0
    cutoff = time.time() - min_age_min * 60
    removed = 0
    for child in sorted(inbox.iterdir()):
        if not child.is_dir() or child.name.startswith((".", "_")):
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


def main(argv=None) -> int:
    setup_logging()
    min_age = cfg.RECONCILE_IMPORT_MIN_AGE_MIN
    run_id = "import-" + time.strftime("%Y%m%d-%H%M%S")

    log("===== reconcile-import start =====")
    log(f"inbox={cfg.INBOX_DIR} min_age_min={min_age} run_id={run_id}")

    _ledger_poll()

    pruned = _prune_empty_dirs(Path(str(cfg.INBOX_DIR)), min_age)
    if pruned:
        log(f"[CLEANUP] pruned {pruned} empty inbox dir(s)")

    try:
        rc = reconcile.main(
            ["--inbox", "--execute", "--min-age-min", str(min_age), "--run-id", run_id]
        )
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

    if new or upg:
        _plex_refresh()
    else:
        log("[PLEX] no library changes — refresh skipped")

    # Surface anything imported or needing review, but never fail the unit on a
    # park (a park is the gate doing its job, not an outage).
    if new or upg or park:
        pipeline_db.push_notification(
            "reconcile_import", run_id,
            new=new, upgrade=upg, duplicate=dup, park=park, run_id=run_id,
        )

    log("===== reconcile-import done =====")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
