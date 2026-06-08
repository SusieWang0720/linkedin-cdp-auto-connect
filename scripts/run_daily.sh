#!/usr/bin/env bash
# run_daily.sh — wrapper invoked by launchd: ensures Chrome is up, then sends today's batch.
set -euo pipefail
SKILL_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-/opt/anaconda3/bin/python3}"
OUT_DIR="${OUT_DIR:-$HOME/.linkedin-outreach}"
DAILY="${DAILY:-10}"

mkdir -p "$OUT_DIR/runs"
LOG="$OUT_DIR/runs/launchd_$(date +%Y%m%d_%H%M%S).log"

bash "$SKILL_DIR/scripts/start_chrome.sh" >>"$LOG" 2>&1

"$PYTHON" "$SKILL_DIR/scripts/send_daily.py" \
  --out-dir "$OUT_DIR" \
  --daily "$DAILY" \
  >>"$LOG" 2>&1

echo "Log: $LOG"
