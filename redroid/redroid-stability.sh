#!/usr/bin/env bash
# redroid-stability.sh — host-side supervisor that keeps a redroid container drivable
# on a KVM-less host. Without it, the boot-time lmkd instance comes up broken (epoll
# EINVAL busy-loop and/or never accepts system_server's socket); an unreachable lmkd
# makes system_server block ~3s per process start under the global AMS lock, so GMS-heavy
# screens (login, Timeline) wedge the framework and adbd appears to "die".
#
# It converges the container every INTERVAL seconds: restarts lmkd if it spins or loses
# its AMS socket, silences the lmkd log flood, keeps the display awake (headless driving
# needs it), and keeps the crash-looping simulated bluetooth HAL down.
#
# Run under systemd (see setup.sh) or standalone:  RD_CONTAINER=rd ./redroid-stability.sh
set -uo pipefail
CONTAINER="${RD_CONTAINER:-rd}"
INTERVAL="${RD_STABILITY_INTERVAL:-30}"
NO_ESTAB_STRIKES=0

dx() { docker exec "$CONTAINER" sh -c "$1" 2>/dev/null; }
log() { echo "$(date '+%F %T') [$CONTAINER] $*"; }
# Android init has NO `restart` shell verb (it errors 127 and is a no-op); only
# stop+start actually recreates the process.
restart_lmkd() { dx 'stop lmkd'; sleep 1; dx 'start lmkd'; }

log "supervisor started"
while true; do
    if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" != "true" ] \
       || [ "$(dx 'getprop sys.boot_completed')" != "1" ]; then
        NO_ESTAB_STRIKES=0; sleep "$INTERVAL"; continue
    fi

    # 1. silence the lmkd EINVAL log flood (logd otherwise burns a core during spins)
    [ "$(dx 'getprop log.tag.lowmemorykiller')" != "S" ] && { dx 'setprop log.tag.lowmemorykiller S'; log "set log.tag.lowmemorykiller=S"; }

    # 2. lmkd health: exists, not spinning, holds an ESTAB socket to AMS
    LMKD_PID=$(dx 'pidof lmkd')
    if [ -z "$LMKD_PID" ]; then
        dx 'start lmkd'; log "lmkd was not running — started"
    else
        A=$(dx "awk '{print \$14+\$15}' /proc/$LMKD_PID/stat"); sleep 3
        B=$(dx "awk '{print \$14+\$15}' /proc/$LMKD_PID/stat")
        if [ -n "$A" ] && [ -n "$B" ] && [ $((B - A)) -gt 240 ]; then
            restart_lmkd; log "lmkd busy-looping (${A}->${B} ticks/3s) — restarted"
            NO_ESTAB_STRIKES=0; sleep "$INTERVAL"; continue
        fi
        if [ "$(dx 'ss -x' | grep -c 'ESTAB.*lmkd')" -eq 0 ]; then
            NO_ESTAB_STRIKES=$((NO_ESTAB_STRIKES + 1))
            if [ "$NO_ESTAB_STRIKES" -ge 2 ]; then
                restart_lmkd; log "lmkd had no AMS connection twice — restarted"; NO_ESTAB_STRIKES=0
            fi
        else
            NO_ESTAB_STRIKES=0
        fi
    fi

    # 3. keep display awake (asleep = black screencaps + activities pausing into limbo).
    #    svc power stayon is a no-op here (fake battery = unplugged), so use max timeout.
    [ "$(dx 'settings get system screen_off_timeout')" != "2147483647" ] && { dx 'settings put system screen_off_timeout 2147483647'; log "set screen_off_timeout=max"; }
    dx 'dumpsys power' | grep -q 'mWakefulness=Asleep' && { dx 'input keyevent KEYCODE_WAKEUP'; log "display was asleep — woke it"; }

    # 4. the simulated bluetooth HAL only SIGABRT-loops (no hardware) — keep it down
    [ "$(dx 'getprop init.svc.vendor.bluetooth-1-1')" = "running" ] && { dx 'stop vendor.bluetooth-1-1'; log "stopped vendor.bluetooth-1-1"; }

    sleep "$INTERVAL"
done
