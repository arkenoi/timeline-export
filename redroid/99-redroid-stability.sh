#!/system/bin/sh
# Magisk service.d boot script — the in-container half of the redroid stability fix.
# Persists in the /data volume and runs at each boot (needs /data/adb/magisk/busybox
# populated; some redroid images ship that dir empty — setup.sh copies it in).
# The host supervisor (redroid-stability.sh) is the belt to this braces.

LOG=/data/local/tmp/stability.log
echo "$(date) service.d script start" >> $LOG

# silence the lmkd EINVAL log flood at the source
setprop log.tag.lowmemorykiller S

until [ "$(getprop sys.boot_completed)" = "1" ]; do sleep 2; done
sleep 5

# daemons broken at boot. Android init has NO `restart` verb (no-op) — use stop+start.
stop lmkd; sleep 1; start lmkd
stop vendor.bluetooth-1-1 2>/dev/null   # SIGABRT loop (simulated HAL, no HW)

# headless driving needs the display awake; svc power stayon is a no-op (fake battery),
# so a huge screen-off timeout is the effective keep-awake.
settings put system screen_off_timeout 2147483647
input keyevent KEYCODE_WAKEUP
echo "$(date) applied: lmkd pid=$(pidof lmkd)" >> $LOG

# self-heal: restart lmkd if it re-enters the busy-loop (>~80% CPU over 5s)
(
  while true; do
    sleep 60
    p=$(pidof lmkd); [ -z "$p" ] && continue
    a=$(awk '{print $14+$15}' /proc/$p/stat 2>/dev/null); sleep 5
    b=$(awk '{print $14+$15}' /proc/$p/stat 2>/dev/null)
    [ -z "$a" ] || [ -z "$b" ] && continue
    if [ $((b - a)) -gt 400 ]; then
      echo "$(date) lmkd spinning (ticks5s=$((b - a))), restarting" >> $LOG
      stop lmkd; sleep 1; start lmkd
    fi
  done
) &
