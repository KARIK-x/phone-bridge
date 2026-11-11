#!/usr/bin/env bash
# Stop the bridge (does not touch tmux/OpenCode)
set -euo pipefail
pkill -f "python3 bridge.py" 2>/dev/null || true
pkill -f "cloudflared tunnel" 2>/dev/null || true
echo "bridge/tunnel stopped (tmux session left running)"