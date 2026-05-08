#!/bin/bash
set -u

LOG_FILE="/var/log/music-pipeline-health.log"
BEETS_LOG="/var/log/beets-import.log"
COMPLETE_DIR="/mnt/scratch/slskd/complete"
INCOMPLETE_DIR="/mnt/scratch/slskd/incomplete"
QUARANTINE_DIR="/mnt/scratch/slskd/quarantine"
CHECKER="/usr/local/bin/check_chromaprint.py"
IMPORTER="/usr/local/bin/beets-import.sh"
WARN_STAMP="/var/lib/music-pipeline-healthcheck/warn-notified.stamp"
WARN_COOLDOWN_H=24
WARN_COUNT=0
FAIL_COUNT=0

now() { date "+%Y-%m-%d %H:%M:%S"; }

log_line() {
    local level="$1"
    shift
    printf "%s [%s] %s\n" "$(now)" "$level" "$*" >> "$LOG_FILE"
}

warn() {
    WARN_COUNT=$((WARN_COUNT + 1))
    log_line "WARN" "$*"
}

fail() {
    FAIL_COUNT=$((FAIL_COUNT + 1))
    log_line "FAIL" "$*"
}

ok() {
    log_line "OK" "$*"
}

notify_telegram() {
    local msg="$1"
    HEALTHCHECK_MSG="$msg" python3 - << 'PY' 2>/dev/null
import os, sys
sys.path.insert(0, '/usr/local/bin')
import pipeline_db
pipeline_db.init_db()
pipeline_db.push_notification('healthcheck_alert', '', message=os.environ['HEALTHCHECK_MSG'])
PY
}

check_exists_exec() {
    local path="$1"
    if [ -x "$path" ]; then
        ok "executable present: $path"
    else
        fail "missing or not executable: $path"
    fi
}

check_dir_exists() {
    local path="$1"
    if [ -d "$path" ]; then
        ok "directory present: $path"
    else
        fail "missing directory: $path"
    fi
}

check_disk_threshold() {
    local path="$1"
    local label="$2"
    local pct
    pct=$(df -P "$path" 2>/dev/null | awk "NR==2 {gsub(/%/, \"\", \$5); print \$5}")
    if [ -z "$pct" ]; then
        warn "disk usage unavailable for $label ($path)"
        return
    fi
    if [ "$pct" -ge 95 ]; then
        fail "disk critically high on $label: ${pct}% used"
    elif [ "$pct" -ge 85 ]; then
        warn "disk high on $label: ${pct}% used"
    else
        ok "disk healthy on $label: ${pct}% used"
    fi
}

check_timer_active() {
    local timer="$1"
    if systemctl is-active --quiet "$timer"; then
        ok "timer active: $timer"
    else
        fail "timer inactive: $timer"
    fi
}

check_beets_stale_runtime() {
    local pids
    local stale=0
    pids=$(pgrep -f "/usr/local/bin/beets-import.sh" || true)
    if [ -z "$pids" ]; then
        ok "beets importer not currently running (normal between timer runs)"
        return
    fi

    for pid in $pids; do
        local etime
        etime=$(ps -p "$pid" -o etimes= 2>/dev/null | tr -d " ")
        if [ -n "$etime" ] && [ "$etime" -gt 14400 ]; then
            stale=1
            warn "beets importer pid=$pid running for ${etime}s (>4h)"
        fi
    done

    if [ "$stale" -eq 0 ]; then
        ok "beets importer runtime within expected bounds"
    fi
}

# slskd-recover is a batch script, not a daemon — not running is normal.
# Only flag if it has been failing (exited non-zero recently).
check_recover_status() {
    local result
    result=$(systemctl show slskd-recover.service --property=Result 2>/dev/null | cut -d= -f2 || true)
    if [ "$result" = "exit-code" ] || [ "$result" = "signal" ]; then
        warn "slskd-recover last run failed (Result=$result)"
    else
        ok "slskd-recover last run ok (Result=${result:-unknown})"
    fi
}

check_backlog() {
    # ── complete/ dir ──────────────────────────────────────────────────────────
    if [ ! -d "$COMPLETE_DIR" ]; then
        fail "cannot inspect complete/ backlog — missing $COMPLETE_DIR"
    else
        local total_count stuck_count
        total_count=$(find "$COMPLETE_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
        # Folders older than 12h that haven't been promoted are genuinely stuck
        stuck_count=$(find "$COMPLETE_DIR" -mindepth 1 -maxdepth 1 -type d -mmin +720 2>/dev/null | wc -l)
        if [ "$stuck_count" -gt 5 ]; then
            warn "complete/ has ${stuck_count} folder(s) older than 12h (total ${total_count}) — pipeline may be stalled"
        else
            ok "complete/ backlog: total=${total_count}, stuck_12h=${stuck_count}"
        fi
    fi

    # ── incomplete/ dir ────────────────────────────────────────────────────────
    if [ -d "$INCOMPLETE_DIR" ]; then
        local inc_total inc_old
        inc_total=$(find "$INCOMPLETE_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
        # Stalled >72h without being active in slskd is worth flagging
        inc_old=$(find "$INCOMPLETE_DIR" -mindepth 1 -maxdepth 1 -type d -mmin +4320 2>/dev/null | wc -l)
        if [ "$inc_total" -gt 20 ]; then
            warn "incomplete/ has ${inc_total} folder(s) — ${inc_old} older than 72h"
        else
            ok "incomplete/ backlog: total=${inc_total}, older_than_72h=${inc_old}"
        fi
    fi

    # ── quarantine/ dir ────────────────────────────────────────────────────────
    if [ -d "$QUARANTINE_DIR" ]; then
        local q_count
        q_count=$(find "$QUARANTINE_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
        if [ "$q_count" -gt 30 ]; then
            warn "quarantine growing: ${q_count} dirs"
        else
            ok "quarantine size acceptable: ${q_count} dirs"
        fi
    fi
}

check_held_folders() {
    python3 - << 'PY' 2>/dev/null
import sys, time
sys.path.insert(0, '/usr/local/bin')
try:
    import pipeline_db
    pipeline_db.init_db()
    state = pipeline_db.get_held_folders()
except Exception as e:
    print(f"WARN: could not read held folders from DB: {e}")
    sys.exit(0)

now = time.time()
held_long = []
for name, entry in state.items():
    age_h = (now - entry['first_seen']) / 3600
    if age_h >= 24:
        held_long.append((age_h, name))

if held_long:
    held_long.sort(reverse=True)
    print(f"WARN: {len(held_long)} folder(s) held incomplete for >=24h:")
    for age_h, name in held_long[:5]:
        print(f"  {age_h:.0f}h: {name}")
    if len(held_long) > 5:
        print(f"  ... and {len(held_long) - 5} more")
else:
    count = len(state)
    print(f"OK: {count} held folder(s), all <24h old")
PY

    local py_out
    py_out=$(python3 - << 'PY2' 2>/dev/null
import sys, time
sys.path.insert(0, '/usr/local/bin')
try:
    import pipeline_db
    pipeline_db.init_db()
    state = pipeline_db.get_held_folders()
except Exception:
    sys.exit(0)
now = time.time()
long_held = [v for v in state.values() if (now - v['first_seen']) / 3600 >= 24]
print(len(long_held))
PY2
)
    if [ -n "$py_out" ] && [ "$py_out" -gt 0 ] 2>/dev/null; then
        warn "held-folders: ${py_out} folder(s) stuck >=24h in complete/ (fill-missing-tracks will attempt repairs)"
    else
        ok "held-folders: all within 24h threshold"
    fi
}

check_recent_errors() {
    if [ ! -f "$BEETS_LOG" ]; then
        warn "beets log not found: $BEETS_LOG"
        return
    fi

    # Only count genuine error lines — not normal output that contains the word "error"
    local recent_err_count
    recent_err_count=$(tail -n 500 "$BEETS_LOG" | grep -Ec "ERROR|Traceback \(most recent|CRITICAL|beet import command failed|chromaprint.*failed|quality.*failed")
    if [ "$recent_err_count" -gt 0 ]; then
        warn "beets log has ${recent_err_count} error line(s) in last 500 lines"
    else
        ok "no errors in recent beets log window"
    fi

    # Check pipeline isn't idle — last import cycle should have run within 2h
    local last_cycle_age
    last_cycle_age=$(awk '/Import cycle finished|No files in import dir/{last=$1" "$2} END{print last}' "$BEETS_LOG" 2>/dev/null | \
        python3 -c "
import sys, time
from datetime import datetime
line = sys.stdin.read().strip()
if not line:
    print(9999)
    sys.exit()
try:
    dt = datetime.strptime(line, '%Y-%m-%d %H:%M:%S')
    print(int((time.time() - dt.timestamp()) / 60))
except Exception:
    print(9999)
" 2>/dev/null)
    if [ -z "$last_cycle_age" ]; then
        last_cycle_age=9999
    fi
    if [ "$last_cycle_age" -gt 120 ]; then
        warn "beets-import last ran ${last_cycle_age}min ago (>2h) — timer may be broken"
    else
        ok "beets-import last ran ${last_cycle_age}min ago"
    fi
}

check_failed_units() {
    local failed_units
    failed_units=$(systemctl --failed --no-legend --plain 2>/dev/null | wc -l)
    if [ "$failed_units" -gt 0 ]; then
        warn "system has ${failed_units} failed systemd unit(s)"
    else
        ok "no failed systemd units"
    fi
}

TEMP_WARN=85
TEMP_CRITICAL=92

get_max_temp() {
    cat /sys/class/thermal/thermal_zone*/temp 2>/dev/null \
        | awk 'BEGIN{max=0} {t=$1/1000; if(t>20 && t<120 && t>max) max=t} END{printf "%.0f", max}'
}

check_temperature() {
    local max_temp
    max_temp=$(get_max_temp)

    if [ -z "$max_temp" ] || [ "$max_temp" -eq 0 ]; then
        warn "could not read CPU temperature"
        return
    fi

    if [ "$max_temp" -ge "$TEMP_CRITICAL" ]; then
        fail "CPU critically hot: ${max_temp}°C — stopping pipeline processes"
        systemctl stop beets-import.service 2>/dev/null || true
        pkill -f "/usr/local/bin/slskd-recover.py" 2>/dev/null || true
        log_line "FAIL" "beets-import stopped and slskd-recover killed due to thermal emergency"
    elif [ "$max_temp" -ge "$TEMP_WARN" ]; then
        warn "CPU temperature elevated: ${max_temp}°C"
    else
        ok "CPU temperature healthy: ${max_temp}°C"
    fi
}

# ── Run checks ────────────────────────────────────────────────────────────────

log_line "INFO" "===== Music pipeline healthcheck start ====="

check_temperature
check_exists_exec "$CHECKER"
check_exists_exec "$IMPORTER"
check_dir_exists "$COMPLETE_DIR"
check_dir_exists "$QUARANTINE_DIR"

# Core pipeline timers
check_timer_active "beets-import.timer"
check_timer_active "music-pipeline-healthcheck.timer"
check_timer_active "slskd-promote-ready.timer"

# Pipeline support timers
check_timer_active "slskd-incomplete-watchdog.timer"
check_timer_active "slskd-fill-missing-tracks.timer"
check_timer_active "slskd-quarantine-requeue.timer"
check_timer_active "slskd-wishlist-check.timer"
check_timer_active "pipeline-weekly-digest.timer"

check_disk_threshold "/mnt/scratch" "scratch"
check_disk_threshold "/mnt/storage" "storage"

check_recover_status
check_beets_stale_runtime
check_backlog
check_held_folders
check_recent_errors
check_failed_units

# ── Summary + Telegram alert on failures ─────────────────────────────────────

if [ "$FAIL_COUNT" -gt 0 ]; then
    log_line "FAIL" "healthcheck completed with failures: fail=${FAIL_COUNT} warn=${WARN_COUNT}"
    notify_telegram "FAIL music-pipeline healthcheck: ${FAIL_COUNT} failure(s), ${WARN_COUNT} warning(s). Check /var/log/music-pipeline-health.log"
    log_line "INFO" "===== Music pipeline healthcheck end ====="
    exit 2
fi

if [ "$WARN_COUNT" -gt 0 ]; then
    log_line "WARN" "healthcheck completed with warnings: fail=${FAIL_COUNT} warn=${WARN_COUNT}"
    # Only notify once per cooldown window to avoid repeated Telegram spam
    mkdir -p "$(dirname "$WARN_STAMP")"
    _should_notify=1
    if [ -f "$WARN_STAMP" ]; then
        _stamp_age=$(( ( $(date +%s) - $(date -r "$WARN_STAMP" +%s) ) / 3600 ))
        if [ "$_stamp_age" -lt "$WARN_COOLDOWN_H" ]; then
            _should_notify=0
            log_line "INFO" "warn notification suppressed (last sent ${_stamp_age}h ago, cooldown=${WARN_COOLDOWN_H}h)"
        fi
    fi
    if [ "$_should_notify" -eq 1 ]; then
        notify_telegram "WARN music-pipeline healthcheck: ${WARN_COUNT} warning(s). Check /var/log/music-pipeline-health.log"
        touch "$WARN_STAMP"
    fi
else
    log_line "OK" "healthcheck completed clean: fail=${FAIL_COUNT} warn=${WARN_COUNT}"
    # Clear stamp so next warning cycle gets a fresh notification
    rm -f "$WARN_STAMP"
fi

log_line "INFO" "===== Music pipeline healthcheck end ====="
exit 0
