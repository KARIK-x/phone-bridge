#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/launchd/com.ashim.ai-bridge.plist"
DST="$HOME/Library/LaunchAgents/com.ashim.ai-bridge.plist"
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__PYTHON__|$(command -v python3)|" -e "s|__BRIDGE_DIR__|$ROOT|g" "$SRC" > "$DST"
launchctl unload "$DST" 2>/dev/null || true
launchctl load "$DST"
echo "installed: $DST"
