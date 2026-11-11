#!/usr/bin/env bash
PLIST="$HOME/Library/LaunchAgents/com.ashim.ai-bridge.plist"
launchctl unload "$PLIST" 2>/dev/null || true
rm -f "$PLIST"
echo "uninstalled"
