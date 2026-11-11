# Chat-to-Terminal Bridge - Setup

## 1. Install
cd ~/phone-bridge
cp .env.example .env
chmod +x scripts/*.sh

## 2. Create tmux session (manual test)
bash scripts/start-tmux.sh
tmux attach -t ai-bridge     # verify OpenCode runs; detach Ctrl+B then D

## 3. Telegram bot (recommended, ~15 min)
1. Message @BotFather -> /newbot -> name + username -> copy the token.
   Put in .env: TELEGRAM_BOT_TOKEN=...
2. Get your user id: message @userinfobot -> it replies with your id.
   Put in .env: ALLOWED_CHAT_ID=<that id>
   (or leave empty and pair later with PAIR_TOKEN)
3. Set PAIR_TOKEN to a long random string (openssl rand -hex 32).
4. Default mode is polling -> no tunnel needed. The bot calls Telegram.

## 4. WhatsApp (optional, 1-2 hrs)
1. developers.facebook.com -> create app -> add WhatsApp.
2. WhatsApp -> API Setup: copy Phone number ID -> WHATSAPP_PHONE_NUMBER_ID.
3. Create System User token with whatsapp_business_messaging -> WHATSAPP_ACCESS_TOKEN.
4. Choose WHATSAPP_VERIFY_TOKEN. Set BACKEND=whatsapp.
5. (Recommended) App Dashboard -> Settings -> Basic -> App Secret -> WHATSAPP_APP_SECRET (enables X-Hub-Signature-256 verification).
6. WhatsApp requires a public webhook -> use Cloudflare Tunnel (below).

## 5. Cloudflare Tunnel (only needed for WhatsApp or Telegram webhook mode)
Cloudflare (free):
  cloudflared tunnel --url http://localhost:3000
  # prints https://....trycloudflare.com
For a stable URL, create a named tunnel:
  cloudflared tunnel login
  cloudflared tunnel create ai-bridge
  cloudflared tunnel route dns ai-bridge bridge.yourdomain.com
  cloudflared tunnel run ai-bridge
ngrok (alternative): ngrok http 3000

## 6. Telegram webhook mode (optional)
Set TELEGRAM_MODE=webhook, then:
  curl "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook?url=https://YOUR-TUNNEL/webhook&secret_token=$TELEGRAM_WEBHOOK_SECRET"
WhatsApp webhook: Meta app -> WhatsApp -> Configuration -> Webhook:
  Callback URL: https://YOUR-TUNNEL/webhook
  Verify token: your WHATSAPP_VERIFY_TOKEN
  Subscribe to "messages".

## 7. WhatsApp test recipient (required for development)
Meta restricts outbound messages to numbers in your test recipient list.
1. Meta Developer Console -> your app -> WhatsApp -> API Setup.
2. "To" / Recipient phone numbers -> Add phone number.
3. Enter your personal WhatsApp number and verify via OTP.

## 8. Run
python3 bridge.py
# or as a service:
bash scripts/install-launchd.sh

## 8. Pair (if ALLOWED_CHAT_ID empty)
Send: /pair <your PAIR_TOKEN>   -> "Paired successfully."

## 9. Test from phone
/status
/tail 20
hello, what files are in this directory?
/approve
/deny
/stop

## Security notes
- Only ALLOWED_CHAT_ID (or paired id) gets responses; others silently ignored.
- No arbitrary shell execution; only allowlisted slash commands reach tmux.
- Keep .env and state.json private (never commit).
- Tunnel URL changes on restart unless using a named tunnel.

## Remote control features
- Normal messages become OpenCode instructions; if busy, they are queued (not lost).
- Commands: /status /tail N /approve /deny /stop /queue /clear /help
- /approve only sends Enter when a permission prompt is detected in the pane.
- Proactive notifications: task start, finish, failure, permission, restart (configurable via NOTIFY_*).
- Task state and queue persist in state.json (survives restarts).

## Production 24/7 deployment

### System User token (recommended, permanent)
Temporary tokens expire in 24h. For 24/7, create a System User token:
1. Meta Business Manager -> Settings -> Users -> System Users -> Create System User.
2. Assign the System User to your app with whatsapp_business_messaging permission.
3. Generate a token for the System User.
4. Update .env: WHATSAPP_ACCESS_TOKEN=<system user token>.
5. Restart bridge.

### Named Cloudflare Tunnel (stable URL)
1. cloudflared tunnel login
2. cloudflared tunnel create ai-bridge
3. cloudflared tunnel route dns ai-bridge bridge.yourdomain.com
4. Copy cloudflared/config.example.yml to ~/.cloudflared/config.yml, fill in tunnel ID.
5. cloudflared tunnel run ai-bridge
6. Update Meta webhook callback URL to https://bridge.yourdomain.com/webhook.

### LaunchAgents (prepared, not installed)
- Bridge: bash scripts/install-launchd.sh (uses launchd/com.ashim.ai-bridge.plist)
- Tunnel: copy launchd/com.ashim.ai-bridge-tunnel.plist to ~/Library/LaunchAgents/ and launchctl load.
- caffeinate -s is included in the bridge plist (prevents sleep on AC power).

### Health and diagnostics
- bash scripts/health-check.sh
- bash scripts/diagnose.sh
- tail -f bridge.log

## Auto-restart
- launchd keeps the bridge alive (KeepAlive).
- Bridge recreates the tmux session if it dies (10s watchdog).
- OpenCode runs in a restart loop inside tmux.
