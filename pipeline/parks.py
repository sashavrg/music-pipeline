#!/usr/bin/env python3
"""
parks.py — visibility into the fail-to-review outbox.

Parks are the pipeline's safety valve: anything the gate refuses to write
lands in PARK_DIR/<run-id>/<folder> with a reason in that run's plan.json.
A safety valve nobody looks at is where albums go to disappear, so this
module gives the parked set one queryable surface:

    python3 -m pipeline.parks          # table, oldest first
    (weekly_digest imports collect_parks for the Telegram summary)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from . import config as cfg

PARK_ROOT = Path(str(cfg.SCRATCH_ROOT)) / "_parked"
PLAN_ROOT = Path(str(cfg.LOG_DIR)) / "reconcile"


def _plan_reasons(run_id: str) -> dict:
    """folder-basename -> route_reason for one run, {} when no plan exists."""
    plan = PLAN_ROOT / run_id / "plan.json"
    try:
        entries = json.loads(plan.read_text(encoding="utf-8")).get("entries", [])
    except Exception:
        return {}
    out = {}
    for e in entries:
        path = (e.get("candidate") or {}).get("path", "")
        if path:
            out[os.path.basename(path)] = e.get("route_reason", "")
    return out


def collect_parks(root: Path = None) -> list[dict]:
    """Every parked folder: name, run_id, age_days, n_files, reason (from the
    run's plan.json when it still exists). Sorted oldest first."""
    root = root or PARK_ROOT
    if not root.is_dir():
        return []
    now = time.time()
    parks = []
    for run_dir in sorted(root.iterdir()):
        if not run_dir.is_dir():
            continue
        reasons = _plan_reasons(run_dir.name)
        for folder in sorted(run_dir.iterdir()):
            if not folder.is_dir():
                continue
            try:
                mtime = folder.stat().st_mtime
                n_files = sum(1 for p in folder.rglob("*") if p.is_file())
            except OSError:
                continue
            parks.append({
                "name": folder.name,
                "run_id": run_dir.name,
                "age_days": (now - mtime) / 86400.0,
                "n_files": n_files,
                "reason": reasons.get(folder.name, ""),
            })
    parks.sort(key=lambda p: -p["age_days"])
    return parks


def main(argv=None) -> int:
    parks = collect_parks()
    if not parks:
        print("no parked folders")
        return 0
    print(f"{len(parks)} parked folder(s), oldest first "
          f"({sum(p['n_files'] for p in parks)} files total):\n")
    for p in parks:
        print(f"  {p['age_days']:5.1f}d  {p['n_files']:3d}f  "
              f"[{p['run_id']}]  {p['name']}")
        if p["reason"]:
            print(f"         reason: {p['reason']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
