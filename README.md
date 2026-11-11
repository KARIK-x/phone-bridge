# WhatsApp-to-OpenCode Remote AI Workstation

Control your Mac's OpenCode development environment from anywhere via WhatsApp.

Send natural-language instructions, manage OpenCode sessions/models/agents, queue tasks, monitor progress, and receive completion notifications — all from your phone.

## Features

- **Natural language control** — any WhatsApp message becomes an OpenCode instruction
- **Session management** — list, create, switch OpenCode sessions via `/sessions`, `/session`
- **Model & agent selection** — discover and switch models (`/models`, `/model use`), agents (`/agents`, `/agent`), thinking levels (`/variants`, `/think`)
- **Task queue** — queue follow-up instructions while busy; process sequentially
- **Proactive notifications** — task start, completion, failure, permission requests, restart
- **Permission handling** — `/approve` and `/deny` only act when a real permission prompt is detected
- **Security** — sender allowlist, HMAC-SHA256 webhook verification, duplicate detection, no arbitrary shell execution
- **Reliability** — persistent tmux session, watchdog, launchd auto-restart, structured logging, health checks
- **OpenCode Server API** — uses official `opencode serve` API for structured session/model/agent management
- **WhatsApp Cloud API** — full webhook verification, signature validation, chunked message sending

## Architecture

```
Phone (WhatsApp) -> Meta Cloud API -> Cloudflare Tunnel (HTTPS)
  -> Mac: bridge.py HTTP server (:3000)
    -> Security (allowlist + HMAC-SHA256 + dedup)
    -> Message router (commands vs free-form)
    -> Task queue (persistent JSON state)
    -> OpenCode Server API (localhost:4096, password-protected)
    -> tmux (persistent terminal, manual use)
    -> Output monitor + notifications
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for details.

## Commands

| Command | Description |
|---|---|
| `/status` | Session state, task, queue, last activity, model, agent |
| `/tail N` | Last N lines of terminal output |
| `/sessions` | List OpenCode sessions |
| `/session current\|new\|<id>` | View/create/switch sessions |
| `/models [provider]` | List available models (grouped by provider) |
| `/model current\|use <id>` | View or set active model |
| `/variants` | List reasoning levels for current model |
| `/think <level>` | Set thinking level |
| `/agents` | List available agents |
| `/agent <name>` | Set active agent |
| `/project` | Current project + list projects |
| `/queue` | Pending tasks |
| `/tasks` | Queued tasks |
| `/clear` | Clear queue (with confirmation) |
| `/approve` | Approve permission prompt (only if detected) |
| `/deny` | Reject permission prompt |
| `/stop` | Interrupt current run |
| `/pair <token>` | One-time pairing |
| `/help` | Command list |
| *any text* | Sent to OpenCode as a prompt |

## Requirements

- macOS (Apple Silicon tested)
- Python 3.9+
- tmux
- OpenCode CLI (authenticated with your subscription)
- WhatsApp Cloud API credentials (Meta Developer account)
- Cloudflare Tunnel (for HTTPS webhook)

## Quick start

1. `cd ~/phone-bridge`
2. `cp .env.example .env` and fill in credentials
3. `bash scripts/start-tmux.sh` (creates tmux session running OpenCode)
4. `python3 bridge.py` (starts the bridge)
5. Expose via Cloudflare Tunnel and register webhook in Meta dashboard

See [SETUP.md](SETUP.md) for exact steps.

## Configuration

All configuration via `.env` (see `.env.example`). Key variables:

| Variable | Description |
|---|---|
| `BACKEND` | `whatsapp` or `telegram` |
| `WHATSAPP_PHONE_NUMBER_ID` | Meta phone number ID |
| `WHATSAPP_ACCESS_TOKEN` | Meta access token (System User recommended) |
| `WHATSAPP_VERIFY_TOKEN` | Webhook verification token |
| `WHATSAPP_APP_SECRET` | App secret for HMAC signature verification |
| `ALLOWED_CHAT_ID` | Your WhatsApp number (digits only, no +) |
| `OPENCODE_WORKDIR` | Working directory for OpenCode (default: home) |
| `TMUX_SESSION` | tmux session name (default: ai-bridge) |

## Security

- Only the configured `ALLOWED_CHAT_ID` can control the system
- Webhook signatures verified via HMAC-SHA256 (constant-time comparison)
- Duplicate messages detected and dropped
- No arbitrary shell execution from chat
- Secrets never logged or sent to WhatsApp
- See [SECURITY.md](SECURITY.md)

## Testing

```bash
cd ~/phone-bridge
python3 -m pytest tests/ -q
```

33 tests covering: webhook auth, duplicate prevention, task queue, completion detection, permission handling, notification failures, restart recovery.

## 24/7 Deployment

- LaunchAgent plist: `launchd/com.ashim.ai-bridge.plist` (KeepAlive + RunAtLoad + caffeinate)
- Tunnel: `launchd/com.ashim.ai-bridge-tunnel.plist`
- Named tunnel config: `cloudflared/config.example.yml`
- Health check: `bash scripts/health-check.sh`
- Diagnostics: `bash scripts/diagnose.sh`

## Project structure

```
phone-bridge/
├── bridge.py              # Main application
├── opencode_api.py        # OpenCode Server API client
├── .env.example           # Config template
├── .gitignore
├── requirements.txt       # No dependencies (stdlib only)
├── README.md
├── SETUP.md
├── ARCHITECTURE.md
├── SECURITY.md
├── cloudflared/           # Tunnel config
├── launchd/               # LaunchAgent plists
├── scripts/               # start-tmux, install-launchd, health-check, diagnose, stop
└── tests/                 # Automated tests
```

## License

Personal use.