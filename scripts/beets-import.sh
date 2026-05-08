#!/bin/bash
# Auto-import completed slskd downloads into beets library
# Settled folders -> chromaprint pre-check -> quality-aware prefilter -> beet import.

set -u

IMPORT_DIR="/mnt/scratch/slskd/ready"
QUARANTINE_DIR="/mnt/scratch/slskd/quarantine"
CHECKER="/usr/local/bin/check_chromaprint.py"
QUALITY_UPGRADER="/usr/local/bin/beets-quality-upgrade.py"
STAGED_DELETIONS="/usr/local/bin/beets-apply-staged-deletions.py"
LOOP_STATE_DIR="/var/lib/beets-import"
CLEAN_LIST="$LOOP_STATE_DIR/clean-dirs.txt"
LOG="/var/log/beets-import.log"
MIN_AGE_MINUTES=10
LOOP_STATE_FILE="$LOOP_STATE_DIR/seen-folders.json"
LOOP_WARN_THRESHOLD=3
PIPELINE_LOCK="/var/lib/pipeline/ready-dir.lock"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"
}

# Serialize against slskd-promote-ready: both touch /mnt/scratch/slskd/ready.
mkdir -p "$(dirname "$PIPELINE_LOCK")" "$LOOP_STATE_DIR"
exec 9>"$PIPELINE_LOCK"
if ! flock -w 30 9; then
    log "Could not acquire ready-dir lock within 30s; another pipeline job is running. Skipping cycle."
    exit 0
fi

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

python3 - "$LOOP_STATE_FILE" "$LOOP_WARN_THRESHOLD" "${settled[@]}" <<'PY' 2>/dev/null | while IFS= read -r line; do log "$line"; done
import json, os, sys

sys.path.insert(0, '/usr/local/bin')
try:
    import pipeline_db
    pipeline_db.init_db()
except Exception:
    pipeline_db = None

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
            # Notify once per (folder, count-bucket) — push every Nth cycle past
            # the threshold so we don't spam Telegram every 15 minutes forever.
            # First alert at threshold, then every threshold cycles after that.
            if pipeline_db and (c == threshold or (c - threshold) % threshold == 0):
                try:
                    pipeline_db.push_notification(
                        'beets_import_stuck',
                        os.path.basename(p),
                        consecutive_cycles=c,
                        path=p,
                    )
                except Exception as e:
                    print(f'LOOPGUARD: failed to push notification: {e}')
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
if [ ! -x "$STAGED_DELETIONS" ]; then
    log "ERROR: staged-deletions script missing or not executable at $STAGED_DELETIONS"
    exit 1
fi

# Apply any deletions staged by a previous cycle whose import has since completed.
log "Checking for previously staged deletions to apply"
if ! python3 "$STAGED_DELETIONS" >> "$LOG" 2>&1; then
    log "WARN: apply-staged-deletions reported stale entries — check pending-deletions dir for failed imports"
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
    # Cleanup empties for folders we touched this cycle only — never the whole
    # IMPORT_DIR, since promote-ready may have moved a folder in concurrently
    # (also defended via flock at top of script).
    for d in "${settled[@]}"; do
        [ -d "$d" ] && find "$d" -mindepth 1 -type d -empty -delete
        [ -d "$d" ] && rmdir --ignore-fail-on-non-empty "$d" 2>/dev/null || true
    done
    exit 0
fi

log "Starting beets import - ${#clean[@]} folders"
_pre_import_count=$(sqlite3 /root/.config/beets/library.db "SELECT COUNT(*) FROM items;" 2>/dev/null || echo 0)
if ! beet import -q --quiet-fallback=asis "${clean[@]}" >> "$LOG" 2>&1; then
    log "ERROR: beet import command failed"
    # Still attempt staged deletions check — import may have partially succeeded
    # and some staged files from prior cycles may be safe to apply.
    python3 "$STAGED_DELETIONS" >> "$LOG" 2>&1 || true
    exit 1
fi
_post_import_count=$(sqlite3 /root/.config/beets/library.db "SELECT COUNT(*) FROM items;" 2>/dev/null || echo 0)
_added=$(( _post_import_count - _pre_import_count ))
if [ "$_added" -le 0 ]; then
    log "WARN: beet import ran on ${#clean[@]} folder(s) but 0 items were added to the library — possible silent duplicate discard or failed match"
    # Escape hatch: if a folder has been seen >= LOOP_WARN_THRESHOLD cycles AND
    # the latest beet run added nothing, quarantine it for manual review instead
    # of looping forever. Without this the same folder gets re-imported every
    # 15 min, spamming Telegram via the loopguard alert path.
    python3 - "$LOOP_STATE_FILE" "$LOOP_WARN_THRESHOLD" "$QUARANTINE_DIR" "${clean[@]}" <<'PY' 2>/dev/null | while IFS= read -r line; do log "$line"; done
import json, os, shutil, sys
sys.path.insert(0, '/usr/local/bin')
try:
    import pipeline_db
    pipeline_db.init_db()
except Exception:
    pipeline_db = None

state_file = sys.argv[1]
threshold  = int(sys.argv[2])
quarantine = sys.argv[3]
folders    = sys.argv[4:]

try:
    with open(state_file, 'r', encoding='utf-8') as f:
        seen = json.load(f)
except Exception:
    seen = {}

stuck_dest = os.path.join(quarantine, 'import_stuck')
os.makedirs(stuck_dest, exist_ok=True)

for path in folders:
    p = path.rstrip('/')
    if seen.get(p, 0) < threshold:
        continue
    if not os.path.isdir(p):
        continue
    name = os.path.basename(p)
    target = os.path.join(stuck_dest, name)
    n = 1
    while os.path.exists(target):
        target = os.path.join(stuck_dest, f'{name}__{n}')
        n += 1
    try:
        shutil.move(p, target)
        seen.pop(p, None)
        print(f'STUCK_QUARANTINE: moved {p} -> {target}')
        if pipeline_db:
            try:
                pipeline_db.push_notification(
                    'beets_import_quarantined', name, path=target,
                )
            except Exception as e:
                print(f'STUCK_QUARANTINE: notify failed: {e}')
    except Exception as e:
        print(f'STUCK_QUARANTINE: move failed for {p}: {e}')

with open(state_file, 'w', encoding='utf-8') as f:
    json.dump(seen, f, indent=2, ensure_ascii=False)
PY
else
    log "[VERIFY] beet import added ${_added} item(s) to library (total: ${_post_import_count})"
fi

# Note: do not run global 'beet move' here; it can touch stale DB entries and crash on missing paths.
# Import itself already moves files based on beets config (import.move = yes).

# Apply staged deletions now that import succeeded. The script verifies each
# incoming folder was consumed before touching anything in the library.
log "Applying staged deletions post-import"
if ! python3 "$STAGED_DELETIONS" >> "$LOG" 2>&1; then
    log "WARN: apply-staged-deletions reported stale entries — some old library tracks preserved pending manual review"
fi

for d in "${settled[@]}"; do
    [ -d "$d" ] && find "$d" -mindepth 1 -type d -empty -delete
    [ -d "$d" ] && rmdir --ignore-fail-on-non-empty "$d" 2>/dev/null || true
done
log "Import cycle finished"
