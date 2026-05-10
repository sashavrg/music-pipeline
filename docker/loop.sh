#!/bin/bash
# loop INTERVAL_SECONDS COMMAND [ARGS...]
#
# Replaces systemd's `OnUnitActiveSec=` timer pattern inside docker containers.
# Runs COMMAND, sleeps INTERVAL, repeats. Forwards SIGTERM to the running child
# so `docker compose down` shuts down promptly without waiting for the next
# interval.
#
# Calendar-aligned timers from the host install (wishlist-check at 14:00,
# weekly-digest Sun 20:00) become interval-based in docker — the first run
# fires shortly after container start, the next at INTERVAL seconds later.
set -u
INTERVAL="$1"; shift

cleanup() { kill -TERM "${pid:-0}" 2>/dev/null || true; exit 0; }
trap cleanup TERM INT

while true; do
    "$@" &
    pid=$!
    wait "$pid" || true
    sleep "$INTERVAL" &
    pid=$!
    wait "$pid" || true
done
