#!/usr/bin/env bash
# start_chrome.sh — Launch the dedicated Chrome instance with CDP enabled.
# The user runs this once per session; the profile keeps LinkedIn login.
set -euo pipefail

USER_DATA_DIR="${USER_DATA_DIR:-$HOME/.linkedin-chrome}"
PORT="${PORT:-9222}"
CHROME="${CHROME:-/Applications/Google Chrome.app/Contents/MacOS/Google Chrome}"

mkdir -p "$USER_DATA_DIR"

# If port already up, exit quietly
if curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/json/version" | grep -q '^200$'; then
  echo "CDP already running on :$PORT"
  exit 0
fi

"$CHROME" \
  --remote-debugging-port="$PORT" \
  --user-data-dir="$USER_DATA_DIR" \
  --no-first-run --no-default-browser-check &

# wait until the endpoint is up
for i in {1..30}; do
  sleep 1
  if curl -sS -o /dev/null -w '%{http_code}' "http://127.0.0.1:${PORT}/json/version" | grep -q '^200$'; then
    echo "CDP ready on :$PORT"
    exit 0
  fi
done
echo "CDP did not become ready" >&2
exit 1
