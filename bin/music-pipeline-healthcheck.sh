#!/bin/bash
set -u

# Paths default to the historical host install when env is unset, so existing
# deployments behave identically. Override via /etc/music-pipeline.env or container env.
SCRATCH_ROOT="${SCRATCH_ROOT:-/mnt/scratch/slskd}"
LOG_DIR="${LOG_DIR:-/var/log}"
HEALTHCHECK_STATE_DIR="${HEALTHCHECK_STATE_DIR:-/var/lib/music-pipeline-healthcheck}"

LOG_FILE="$LOG_DIR/music-pipeline-health.log"
BEETS_LOG="$LOG_DIR/beets-import.log"
case "${LOG_TO_STDOUT:-}" in 1|true|yes|on|TRUE|YES|ON) LOG_FILE=/dev/stdout ;; esac
COMPLETE_DIR="${SLSKD_COMPLETE_DIR:-$SCRATCH_ROOT/complete}"
INBOX_DIR="${SLSKD_INBOX_DIR:-$SCRATCH_ROOT/inbox}"
PIPELINE_DB="${PIPELINE_DB:-/var/lib/pipeline/pipeline.db}"
INCOMPLETE_DIR="${SLSKD_INCOMPLETE_DIR:-$SCRATCH_ROOT/incomplete}"
QUARANTINE_DIR="${SLSKD_QUARANTINE_DIR:-$SCRATCH_ROOT/quarantine}"
CHECKER="/usr/local/bin/check-chromaprint"
WARN_STAMP="$HEALTHCHECK_STATE_DIR/warn-notified.stamp"
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
sys.path.insert(0, os.environ.get('MUSIC_PIPELINE_ROOT', '/opt/music-pipeline'))
from pipeline import db as pipeline_db
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
    elif systemctl is-enabled --quiet "$timer" 2>/dev/null; then
        # Enabled but not running = genuinely broken.
        fail "timer enabled but inactive: $timer"
    else
        # Disabled is a deliberate operator choice — e.g. the phased autonomy
        # re-enable after the 2026 spine rebuild, where the acquisition timers
        # come back one at a time behind the slskdq ledger, and beets-import is
        # retired entirely (reconcile is now the sole writer). A disabled timer
        # is not a health failure, so it must not page Telegram every cycle.
        ok "timer disabled (intentional): $timer"
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
    # ── inbox/ dir (the base-function canary) ─────────────────────────────────
    # A settled inbox folder should flow to the library within one or two
    # 15-min reconcile-import cycles. One sitting >24h means the consumer is
    # broken or the folder is invisibly stuck (the Level 42 failure mode) —
    # unless slskd is still downloading into it, which the busy-shield covers
    # and a >24h transfer then trips the ledger check below instead.
    if [ ! -d "$INBOX_DIR" ]; then
        fail "cannot inspect inbox — missing $INBOX_DIR"
    else
        local in_total in_stuck
        in_total=$(find "$INBOX_DIR" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l)
        in_stuck=$(find "$INBOX_DIR" -mindepth 1 -maxdepth 1 -type d -mmin +1440 2>/dev/null | wc -l)
        if [ "$in_stuck" -gt 0 ]; then
            warn "inbox has ${in_stuck} folder(s) older than 24h (total ${in_total}) — downloads may be stranded"
        else
            ok "inbox: total=${in_total}, none older than 24h"
        fi
    fi

    # ── slskd ledger: live rows the poll should have expired ─────────────────
    # STALE_EXPIRE is 48h; a live row past 54h means the scheduled poll is not
    # advancing states (or nothing is running it) — the exact failure that let
    # the first production wishlist row sit 'queued' for 6 days.
    if [ -f "$PIPELINE_DB" ]; then
        local stuck_rows
        stuck_rows=$(sqlite3 "$PIPELINE_DB" \
            "SELECT COUNT(*) FROM slskd_ledger WHERE state IN ('queued','downloading') AND queued_at < strftime('%s','now') - 54*3600;" 2>/dev/null || echo "")
        if [ -z "$stuck_rows" ]; then
            warn "could not query slskd_ledger in $PIPELINE_DB"
        elif [ "$stuck_rows" -gt 0 ]; then
            fail "ledger has ${stuck_rows} live row(s) older than 54h — poll not expiring states"
        else
            ok "ledger: no live rows older than 54h"
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
import os, sys, time
sys.path.insert(0, os.environ.get('MUSIC_PIPELINE_ROOT', '/opt/music-pipeline'))
try:
    from pipeline import db as pipeline_db
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
import os, sys, time
sys.path.insert(0, os.environ.get('MUSIC_PIPELINE_ROOT', '/opt/music-pipeline'))
try:
    from pipeline import db as pipeline_db
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

    # Phantom library matches in the quality-prefilter — usually means a prior
    # `beet import` registered tracks but `beet move` was interrupted, leaving
    # stale DB rows. The prefilter now refuses to delete in this case; surface
    # the event so we know a manual `beet rm` may be needed.
    local quality_warn_count
    quality_warn_count=$(tail -n 1000 "$BEETS_LOG" | grep -Ec "\[QUALITY-WARN\]")
    if [ "$quality_warn_count" -gt 0 ]; then
        warn "quality-prefilter saw ${quality_warn_count} phantom library match(es) — check beets DB for stale rows"
    fi

    # NOTE: the old "beets-import last ran <2h ago, else timer may be broken"
    # idle check was removed in the 2026 spine rebuild. There is no longer a
    # fixed import cadence — reconcile.py is the sole, operator/timer-gated writer
    # and a quiet import dir is the normal steady state, not a fault.
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
        pkill -f "pipeline.recover\|slskd-recover" 2>/dev/null || true
        log_line "FAIL" "slskd-recover killed due to thermal emergency"
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
check_dir_exists "$COMPLETE_DIR"
check_dir_exists "$QUARANTINE_DIR"

# Core pipeline timers
check_timer_active "music-pipeline-healthcheck.timer"
check_timer_active "reconcile-import.timer"

# Pipeline support timers
check_timer_active "slskd-incomplete-watchdog.timer"
check_timer_active "slskd-fill-missing-tracks.timer"
check_timer_active "slskd-quarantine-requeue.timer"
check_timer_active "slskd-wishlist-check.timer"
check_timer_active "pipeline-weekly-digest.timer"

check_disk_threshold "${SCRATCH_DISK_MOUNT:-/mnt/scratch}" "scratch"
check_disk_threshold "${STORAGE_DISK_MOUNT:-/mnt/storage}" "storage"

check_recover_status
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
