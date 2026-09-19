#!/data/data/com.termux/files/usr/bin/bash
cd "$HOME/tabdeal_cloud" || exit 1
LOG="$HOME/tabdeal_cloud/termux_radar_runner.log"
LOCK="$HOME/tabdeal_cloud/.termux_radar.lock"
if ! mkdir "$LOCK" 2>/dev/null; then echo "$(date -u "+%Y-%m-%dT%H:%M:%SZ") SKIP_overlap" >> "$LOG"; exit 0; fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT
echo "$(date -u "+%Y-%m-%dT%H:%M:%SZ") START" >> "$LOG"
python3 -u radar_live.py --once --entry-v1-shadow >> "$LOG" 2>&1
RC=$?
echo "$(date -u "+%Y-%m-%dT%H:%M:%SZ") END rc=$RC" >> "$LOG"
exit "$RC"
