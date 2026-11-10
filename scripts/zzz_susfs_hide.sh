#!/system/bin/sh
# Persist SUSFS sus_path entries across reboots on SukiSU Ultra (built-in SUSFS).
#
# WHY THIS EXISTS:
#   SukiSU's built-in "susfs_manager" module only persists the uname/log toggles,
#   NOT sus_path entries. So paths you add with `ksud susfs add-sus-path` are lost on
#   every reboot unless something re-adds them. Scripts in /data/adb/post-fs-data.d
#   run early on boot, executed by ksud, which is the reliable place to do it.
#
# WHAT IT KILLS:
#   The classic "Su File" detection: an ordinary (non-root) app can stat()
#   /system/bin/su. sus_path makes that path disappear at the VFS layer for
#   non-root processes. (Note: SUSFS deliberately does NOT hide from apps you
#   GRANTED root to — keep detectors/banking apps set to "deny" in the manager.)
#
# INSTALL:
#   adb push zzz_susfs_hide.sh /data/local/tmp/
#   adb shell su -c 'cp /data/local/tmp/zzz_susfs_hide.sh /data/adb/post-fs-data.d/ && \
#     chown root:root /data/adb/post-fs-data.d/zzz_susfs_hide.sh && \
#     chmod 700 /data/adb/post-fs-data.d/zzz_susfs_hide.sh'
#   Then reboot (or run the add-sus-path line once by hand for the current session).

KSUD=/data/adb/ksud

# Hide the on-disk su binary (the "Su File" check):
"$KSUD" susfs add-sus-path /system/bin/su

# Add more paths here if a detector finds other artifacts you want hidden, e.g.:
# "$KSUD" susfs add-sus-path /system/xbin/su
# "$KSUD" susfs add-sus-path /system/bin/magisk
# "$KSUD" susfs add-sus-path /data/adb/ksu
