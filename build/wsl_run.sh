#!/usr/bin/env bash
# Convenience wrapper for running build_13t.sh steps from Windows without quoting trouble:
#   wsl.exe -d Ubuntu-24.04 -u root -- bash /mnt/c/.../build/wsl_run.sh <logname> <WORK> <HMBIRD> <JOBS> <step...>
# Full output goes to /root/logs/<logname>.log; a filtered tail is echoed back.
set -o pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
log=$1; work=$2; hmbird=$3; jobs=$4; shift 4
mkdir -p /root/logs
WORK="$work" HMBIRD="$hmbird" JOBS="$jobs" CLANG="${CLANG:-/root/op13t/zyc-clang}" \
  bash "$here/build_13t.sh" "$@" 2>&1 | tee "/root/logs/$log.log" \
  | grep -v -E '^(Get:|Hit:|Selecting|Preparing|Unpacking|Setting up|Processing|\(Reading)' | tail -n 80
rc=${PIPESTATUS[0]}
echo "pipeline_exit=$rc"
exit "$rc"
