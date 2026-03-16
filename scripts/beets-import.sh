#!/bin/bash
# Auto-import completed slskd downloads into beets library
# Settled folders -> chromaprint pre-check -> quality-aware prefilter -> beet import.

set -u

IMPORT_DIR="/mnt/scratch/slskd/ready"
QUARANTINE_DIR="/mnt/scratch/slskd/quarantine"
CHECKER="/usr/local/bin/check_chromaprint.py"
QUALITY_UPGRADER="/usr/local/bin/beets-quality-upgrade.py"
CLEAN_LIST="/tmp/beets-import-clean-dirs.txt"
LOG="/var/log/beets-import.log"
MIN_AGE_MINUTES=10
LOOP_STATE_DIR="/var/lib/beets-import"
LOOP_STATE_FILE="$LOOP_STATE_DIR/seen-folders.json"
LOOP_WARN_THRESHOLD=3

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"
}

if [ -z "$(ls -A "$IMPORT_DIR" 2>/dev/null)" ]; then
    mkdir -p "$LOOP_STATE_DIR"
    echo '{}' > "$LOOP_STATE_FILE"
    log "No files in import dir; loopguard state reset"
    exit 0
fi

settled=()
for dir in "$IMPORT_DIR"/*/; do
    [ -d "$dir" ] || continue
    if [ -z "$(find "$dir" -type f -mmin -"$MIN_AGE_MINUTES" 2>/dev/null)" ]; then
        settled+=("$dir")
    fi
done

mkdir -p "$LOOP_STATE_DIR"
python3 - "$LOOP_STATE_FILE" "$LOOP_WARN_THRESHOLD" "${settled[@]}" <<'PY' 2>/dev/null | while IFS= read -r line; do log "$line"; done
import json, os, sys
state_file = sys.argv[1]
threshold = int(sys.argv[2])
raw = sys.argv[3:]
current = [p.rstrip('/') for p in raw if p.strip()]

try:
    with open(state_file, 'r', encoding='utf-8') as f:
        prev = json.load(f)
    if not isinstance(prev, dict):
        prev = {}
except Exception:
    prev = {}

new = {}
for p in current:
    new[p] = int(prev.get(p, 0)) + 1

with open(state_file, 'w', encoding='utf-8') as f:
    json.dump(new, f, indent=2, ensure_ascii=False)

if not current:
    print('LOOPGUARD: no settled folders this cycle; state updated')
else:
    max_seen = max(new.values())
    print(f'LOOPGUARD: tracking {len(current)} settled folders; max_consecutive_seen={max_seen}')
    for p in sorted(current):
        c = new[p]
        if c >= threshold:
            print(f'LOOPGUARD: folder seen {c} consecutive cycles -> {p}')
PY

if [ ${#settled[@]} -eq 0 ]; then
    log "No settled folders to import (all modified within last ${MIN_AGE_MINUTES}min)"
    exit 0
fi

if [ ! -x "$CHECKER" ]; then
    log "ERROR: chromaprint checker missing or not executable at $CHECKER"
    exit 1
fi
if [ ! -x "$QUALITY_UPGRADER" ]; then
    log "ERROR: quality upgrader missing or not executable at $QUALITY_UPGRADER"
    exit 1
fi

log "Starting chromaprint pre-check - ${#settled[@]} settled folders"
if ! python3 "$CHECKER" --quarantine-dir "$QUARANTINE_DIR" --clean-output "$CLEAN_LIST" "${settled[@]}" >> "$LOG" 2>&1; then
    log "ERROR: chromaprint pre-check failed"
    exit 1
fi

clean=()
if [ -s "$CLEAN_LIST" ]; then
    mapfile -t clean < "$CLEAN_LIST"
fi
if [ ${#clean[@]} -eq 0 ]; then
    log "No clean folders to import after chromaprint pre-check"
    exit 0
fi

log "Starting quality-aware prefilter on ${#clean[@]} clean folders"
if ! python3 "$QUALITY_UPGRADER" --clean-list "$CLEAN_LIST" >> "$LOG" 2>&1; then
    log "ERROR: quality-aware prefilter failed"
    exit 1
fi

clean=()
if [ -s "$CLEAN_LIST" ]; then
    mapfile -t clean < "$CLEAN_LIST"
fi
if [ ${#clean[@]} -eq 0 ]; then
    log "No folders left to import after quality-aware prefilter"
    # still cleanup empties after deleted incoming duplicates
    find "$IMPORT_DIR" -mindepth 1 -type d -empty -delete
    exit 0
fi

log "Starting beets import - ${#clean[@]} folders"
if ! beet import -q --quiet-fallback=asis "${clean[@]}" >> "$LOG" 2>&1; then
    log "ERROR: beet import command failed"
    exit 1
fi

# Note: do not run global 'beet move' here; it can touch stale DB entries and crash on missing paths.
# Import itself already moves files based on beets config (import.move = yes).

find "$IMPORT_DIR" -mindepth 1 -type d -empty -delete
log "Import cycle finished"
