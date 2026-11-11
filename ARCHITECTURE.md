# Architecture

## Data flow

Phone (WhatsApp) -> Meta Cloud API -> Cloudflare Tunnel -> Mac webhook server (bridge.py) -> security/auth -> message router -> task queue -> OpenCode (tmux) -> output monitor -> WhatsApp notifications.

## Components (bridge.py)

- Config: env vars loaded from .env (stdlib, no python-dotenv).
- State: JSON file (state.json) holding paired chat id, seen message IDs, task queue.
- tmux layer: create/ensure session, send text/keys, interrupt, capture pane.
- Chat backends: Telegram (polling) and WhatsApp (webhook). Unified send_chat().
- Security: sender allowlist, HMAC-SHA256 signature verification (constant-time), duplicate-message detection.
- Task manager: state machine (idle/running/waiting_permission), queue with persistence, completion/failure detection via output patterns.
- Output watcher: polls capture-pane, debounces, diffs, forwards new output, drives task transitions.
- HTTP server: /health, /webhook (GET verify + POST messages).
- Watchdog: recreates tmux session every 10s if dead.

## Task states

RECEIVED (implicit) -> QUEUED -> RUNNING -> WAITING_FOR_APPROVAL -> COMPLETED/FAILED/CANCELLED/INTERRUPTED.

## OpenCode integration

Currently uses tmux interactive TUI:
- Prompts sent via load-buffer/paste-buffer.
- Permission prompts detected via regex patterns on captured pane; /approve sends Enter, /deny sends Ctrl-C.
- Completion detected when output stabilizes (configurable debounce) without permission prompt or error pattern.

Alternative: OpenCode's `serve` mode exposes a local HTTP API (password-protected) with session/message endpoints. This could replace tmux scraping for structured events, but the interactive TUI is retained because it handles permission prompts and preserves the user's existing session. Migration to the serve API is a documented future enhancement.

## Persistence

- state.json: paired chat id, seen message IDs (bounded 1000), task queue.
- SQLite is recommended for a future upgrade (concurrent access, transactions) but JSON is sufficient for single-user use.

## Notifications

Configurable via NOTIFY_ON_* env vars. Sent via send_chat (chunked to 3500 chars). Outbox/retry not yet implemented (noted as limitation).

## Deployment

- LaunchAgent (launchd) with KeepAlive + RunAtLoad.
- caffeinate -s in plist to prevent sleep on AC power.
- Cloudflare Tunnel for HTTPS (quick for testing, named for production).