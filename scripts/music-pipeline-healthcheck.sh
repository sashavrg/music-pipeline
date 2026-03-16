#!/bin/bash
set -u

LOG_FILE="/var/log/music-pipeline-health.log"
BEETS_LOG="/var/log/beets-import.log"
IMPORT_DIR="/mnt/scratch/slskd/complete"
QUARANTINE_DIR="/mnt/scratch/slskd/quarantine"
CHECKER="/usr/local/bin/check_chromaprint.py"
IMPORTER="/usr/local/bin/beets-import.sh"
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

check_recover_running() {
    if pgrep -f "/usr/local/bin/slskd-recover.py" >/dev/null 2>&1; then
        ok "slskd recover process running"
    else
        warn "slskd recover process not running"
    fi
}

check_backlog() {
    if [ ! -d "$IMPORT_DIR" ]; then
        fail "cannot inspect backlog, missing $IMPORT_DIR"
        return
    fi

    local settled_count old_count q_count
    settled_count=$(find "$IMPORT_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
    old_count=$(find "$IMPORT_DIR" -mindepth 1 -maxdepth 1 -type d -mmin +360 2>/dev/null | wc -l)
    q_count=0
    if [ -d "$QUARANTINE_DIR" ]; then
        q_count=$(find "$QUARANTINE_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
    fi

    if [ "$old_count" -gt 200 ]; then
        warn "large old-import backlog: ${old_count} dirs older than 6h (total ${settled_count})"
    else
        ok "import backlog: total_dirs=${settled_count}, older_than_6h=${old_count}"
    fi

    if [ "$q_count" -gt 100 ]; then
        warn "quarantine growing: ${q_count} dirs"
    else
        ok "quarantine size acceptable: ${q_count} dirs"
    fi
}

check_recent_errors() {
    if [ ! -f "$BEETS_LOG" ]; then
        warn "beets log not found: $BEETS_LOG"
        return
    fi

    local recent_err_count
    recent_err_count=$(tail -n 800 "$BEETS_LOG" | grep -Eic "error|traceback|failed|abort|assert|crash")
    if [ "$recent_err_count" -gt 0 ]; then
        warn "recent beets log has ${recent_err_count} suspicious lines in last 800 lines"
    else
        ok "no suspicious keywords in recent beets log window"
    fi

    local latest_summary
    latest_summary=$(grep -F "[SUMMARY]" "$BEETS_LOG" | tail -n 1 || true)
    if [ -n "$latest_summary" ]; then
        ok "latest checker summary: $latest_summary"
    else
        warn "no checker summary found yet in beets log"
    fi
}

check_failed_units() {
    local failed_units
    failed_units=$(systemctl --failed --no-legend --plain 2>/dev/null | wc -l)
    if [ "$failed_units" -gt 0 ]; then
        warn "system has ${failed_units} failed units (not necessarily pipeline-specific)"
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

log_line "INFO" "===== Music pipeline healthcheck start ====="
check_temperature
check_exists_exec "$CHECKER"
check_exists_exec "$IMPORTER"
check_dir_exists "$IMPORT_DIR"
check_dir_exists "$QUARANTINE_DIR"
check_timer_active "beets-import.timer"
check_timer_active "music-pipeline-healthcheck.timer"
check_disk_threshold "/mnt/scratch" "scratch"
check_disk_threshold "/mnt/storage" "storage"
check_recover_running
check_beets_stale_runtime
check_backlog
check_recent_errors
check_failed_units

if [ "$FAIL_COUNT" -gt 0 ]; then
    log_line "FAIL" "healthcheck completed with failures: fail=${FAIL_COUNT} warn=${WARN_COUNT}"
    log_line "INFO" "===== Music pipeline healthcheck end ====="
    exit 2
fi

if [ "$WARN_COUNT" -gt 0 ]; then
    log_line "WARN" "healthcheck completed with warnings: fail=${FAIL_COUNT} warn=${WARN_COUNT}"
else
    log_line "OK" "healthcheck completed clean: fail=${FAIL_COUNT} warn=${WARN_COUNT}"
fi

log_line "INFO" "===== Music pipeline healthcheck end ====="
exit 0
