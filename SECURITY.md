# Security

## Threat model

The webhook is reachable from the internet. Anyone could send a POST to /webhook. The Mac has access to personal files, projects, and credentials via OpenCode.

## Controls implemented

1. Sender allowlist: only ALLOWED_CHAT_ID (or paired id) gets responses; others silently ignored.
2. HMAC-SHA256 signature verification: X-Hub-Signature-256 validated with WHATSAPP_APP_SECRET using hmac.compare_digest (constant-time). Invalid/missing signatures rejected with 403 before processing. If WHATSAPP_APP_SECRET is unset, verification is skipped (backward compat) — set it in production.
3. Webhook verify token: GET /webhook requires hub.verify_token == WHATSAPP_VERIFY_TOKEN.
4. Duplicate-message protection: WhatsApp message IDs tracked in state.json (bounded 1000); duplicates dropped.
5. No arbitrary shell execution: normal text goes to OpenCode via tmux; only allowlisted slash commands (/status, /tail, /approve, /deny, /stop, /queue, /clear, /help, /pair, /tasks) are handled.
6. Secret redaction: credentials never logged or sent to WhatsApp.
7. Request size: Content-Length based read (no hard limit yet — recommended addition).
8. Permission respect: /approve only sends Enter when a permission prompt is detected in the pane; never auto-approves destructive operations.

## Gaps / future hardening

- No request body size limit (add max Content-Length check).
- No rate limiting on /webhook.
- No X-Hub-Signature verification for Telegram (uses secret token header instead).
- Outbound messages are free-form text; Meta's 24h window applies.
- state.json is not encrypted at rest.
- No audit log of executed commands.