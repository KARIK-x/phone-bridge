#!/usr/bin/env python3
"""Phone-to-terminal bridge: control OpenCode from WhatsApp (and Telegram)."""
import hashlib, hmac, json, logging, os, re, subprocess, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

import opencode_api as oc

# ---------- load .env ----------
def _load_env(path):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip(); val = val.strip()
            if (val.startswith('"') and val.endswith('"')) or (val.startswith("'") and val.endswith("'")):
                val = val[1:-1]
            if key and key not in os.environ:
                os.environ[key] = val
_load_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ---------- config ----------
def env(k, d=""): return os.environ.get(k, d)
BACKEND = env("BACKEND", "telegram").lower()
PORT = int(env("PORT", "3000"))
TMUX = env("TMUX_SESSION", "ai-bridge")
OPENCODE_CMD = env("OPENCODE_CMD", "opencode")
WORKDIR = env("OPENCODE_WORKDIR") or os.path.expanduser("~")
if not os.path.isdir(WORKDIR):
    WORKDIR = os.path.expanduser("~")
POLL_MS = float(env("POLL_MS", "1500")) / 1000.0
DEBOUNCE = float(env("DEBOUNCE_MS", "2500")) / 1000.0
COMPLETION_DEBOUNCE = float(env("COMPLETION_DEBOUNCE_MS", "3000")) / 1000.0
TAIL_DEFAULT = int(env("TAIL_DEFAULT", "30"))
CAPTURE_LINES = int(env("CAPTURE_LINES", "500"))
TELEGRAM_MODE = env("TELEGRAM_MODE", "polling").lower()
TELEGRAM_TOKEN = env("TELEGRAM_BOT_TOKEN")
TELEGRAM_SECRET = env("TELEGRAM_WEBHOOK_SECRET")
WA_PHONE_ID = env("WHATSAPP_PHONE_NUMBER_ID")
WA_TOKEN = env("WHATSAPP_ACCESS_TOKEN")
WA_VERIFY = env("WHATSAPP_VERIFY_TOKEN")
WA_APP_SECRET = env("WHATSAPP_APP_SECRET")
HTTP_TIMEOUT = int(env("HTTP_TIMEOUT", "20"))
ALLOWED_CHAT_ID = env("ALLOWED_CHAT_ID")
PAIR_TOKEN = env("PAIR_TOKEN")
STATE_FILE = env("BRIDGE_STATE_FILE", os.path.join(os.path.dirname(__file__), "state.json"))
LOG_FILE = env("LOG_FILE", os.path.join(os.path.dirname(__file__), "bridge.log"))

def _bool(k, d="true"): return env(k, d).lower() == "true"
NOTIFY_ON_START = _bool("NOTIFY_ON_START")
NOTIFY_ON_COMPLETE = _bool("NOTIFY_ON_COMPLETE")
NOTIFY_ON_FAIL = _bool("NOTIFY_ON_FAIL")
NOTIFY_ON_PERMISSION = _bool("NOTIFY_ON_PERMISSION")
NOTIFY_ON_RESTART = _bool("NOTIFY_ON_RESTART")
NOTIFY_TAIL_LINES = int(env("NOTIFY_TAIL_LINES", "15"))

PERMISSION_PATTERNS = [p for p in env("PERMISSION_PATTERNS", r"(?i)(approve|allow|permission)").split(",") if p.strip()]
PERMISSION_REGEXES = [re.compile(p) for p in PERMISSION_PATTERNS]
SEEN_IDS_MAX = 1000

# ---------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
                    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
log = logging.getLogger("bridge")

# ---------- state ----------
def load_state():
    try:
        with open(STATE_FILE) as f: return json.load(f)
    except Exception: return {}
def save_state(s):
    with open(STATE_FILE, "w") as f: json.dump(s, f, indent=2)
def allowed_id(): return ALLOWED_CHAT_ID or load_state().get("paired_chat_id")

# ---------- tmux (kept for manual terminal + /tail) ----------
def tmux(*args):
    return subprocess.run(["tmux", *args], capture_output=True, text=True).stdout
def session_exists():
    return subprocess.run(["tmux", "has-session", "-t", TMUX], capture_output=True).returncode == 0
def create_session():
    if session_exists(): return
    cmd = f"while true; do {OPENCODE_CMD}; sleep 2; done"
    subprocess.run(["tmux", "new-session", "-d", "-s", TMUX, "-c", WORKDIR, cmd])
    subprocess.run(["tmux", "set-option", "-t", TMUX, "history-limit", "100000"], capture_output=True)
def ensure_session():
    if not session_exists(): create_session()
def capture(n=None):
    ensure_session()
    n = n or CAPTURE_LINES
    return subprocess.run(["tmux", "capture-pane", "-t", TMUX, "-p", "-S", f"-{n}"],
                         capture_output=True, text=True).stdout
def interrupt_tmux():
    try: subprocess.run(["tmux", "send-keys", "-t", TMUX, "C-c"], capture_output=True)
    except Exception: pass

# ---------- chat backends ----------
def http_post_json(url, headers, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={**headers, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r: return r.read()
    except urllib.error.HTTPError as e:
        try: detail = e.read().decode("utf-8", "replace")[:300]
        except Exception: detail = ""
        log.error("http err: %s %s", e.code, detail); return None
    except Exception as e:
        log.error("http err: %s", e); return None
def chunks(text, size=3500):
    out = [text[i:i+size] for i in range(0, len(text), size)]
    return out or [""]
def send_telegram(chat_id, text):
    for c in chunks(text):
        http_post_json(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", {}, {"chat_id": chat_id, "text": c})
def send_whatsapp(to, text):
    for c in chunks(text):
        http_post_json(f"https://graph.facebook.com/v20.0/{WA_PHONE_ID}/messages",
                       {"Authorization": f"Bearer {WA_TOKEN}"},
                       {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": c}})
def send_chat(chat_id, text):
    if BACKEND == "telegram": send_telegram(chat_id, text)
    else: send_whatsapp(chat_id, text)
def notify(chat_id, text):
    try: send_chat(chat_id, text)
    except Exception as e: log.error("notify failed: %s", e)

# ---------- security ----------
def verify_wa_signature(sig, raw_body, secret=None):
    secret = secret if secret is not None else WA_APP_SECRET
    if not secret: return True
    if not sig or not isinstance(sig, str) or not sig.startswith("sha256="): return False
    body = raw_body if isinstance(raw_body, (bytes, bytearray)) else str(raw_body).encode("utf-8")
    expected = "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)
def _seen_ids():
    return load_state().get("seen_message_ids", [])
def is_duplicate_message(msg_id):
    if not msg_id: return False
    return msg_id in set(_seen_ids())
def mark_message_seen(msg_id):
    if not msg_id: return
    ids = _seen_ids()
    if msg_id in ids: return
    ids.append(msg_id)
    if len(ids) > SEEN_IDS_MAX: ids = ids[-SEEN_IDS_MAX:]
    st = load_state(); st["seen_message_ids"] = ids; save_state(st)

# ---------- task management ----------
task_state = "idle"
task_start_time = None
task_id_counter = 0
current_task_id = None
permission_notified = False
completion_notified = False
clear_pending_until = 0.0
task_start_idle_ms = None  # baseline idle ms when task started
task_start_outcome = None  # baseline outcome when task started

def get_queue():
    return load_state().get("task_queue", [])
def save_queue(q):
    st = load_state(); st["task_queue"] = q; save_state(st)
def enqueue_task(text):
    q = get_queue()
    q.append({"id": len(q)+1, "text": text, "ts": time.time()})
    save_queue(q); return len(q)
def clear_queue():
    q = get_queue(); n = len(q); save_queue([]); return n
def pop_next_task():
    q = get_queue()
    if not q: return None
    task = q.pop(0); save_queue(q); return task
def detect_permission_prompt(text):
    if not text: return False
    tail = "\n".join(text.strip().splitlines()[-15:])
    return any(rx.search(tail) for rx in PERMISSION_REGEXES)

ERROR_PATTERNS = [p for p in env("ERROR_PATTERNS", r"(?i)(traceback|panic:|error:|fatal:|command not found|exception)").split(",") if p.strip()]
ERROR_REGEXES = [re.compile(p) for p in ERROR_PATTERNS]

def detect_error(text):
    if not text: return False
    tail = "\n".join(text.strip().splitlines()[-20:])
    return any(rx.search(tail) for rx in ERROR_REGEXES)

def get_active_session():
    return oc.get_active_session_id()

def ensure_active_session():
    sid = oc.get_active_session_id()
    if sid:
        try:
            s = oc.get_session(sid)
            if s and s.get("id"): return sid
        except Exception: pass
    # create new
    s = oc.create_session(WORKDIR, title="WhatsApp remote control")
    sid = s.get("id") if isinstance(s, dict) else None
    if sid:
        oc.set_active_session_id(sid)
    return sid

def start_task(text):
    global task_state, task_start_time, current_task_id, task_id_counter
    global permission_notified, completion_notified, task_start_idle_ms
    if not oc.ensure_serve(WORKDIR):
        aid = allowed_id()
        if aid: notify(aid, "OpenCode serve is not available.")
        return False
    sid = ensure_active_session()
    if not sid:
        aid = allowed_id()
        if aid: notify(aid, "Could not create OpenCode session.")
        return False
    task_id_counter += 1
    current_task_id = task_id_counter
    task_state = "running"
    task_start_time = time.time()
    permission_notified = False
    completion_notified = False
    # baseline idle + outcome
    try:
        sess = oc.get_session(sid)
        task_start_idle_ms = oc.session_idle_ms(sess)
        task_start_outcome = sess.get("outcome") if isinstance(sess, dict) else None
    except Exception:
        task_start_idle_ms = None
        task_start_outcome = None
    aid = allowed_id()
    if aid and NOTIFY_ON_START:
        notify(aid, f"Task #{current_task_id} started.")
    # resolve model
    pref = oc.get_preferred_model()
    if not pref:
        picked = oc.pick_default_model(WORKDIR)
        if picked:
            oc.set_preferred_model(picked[0], picked[1], None)
            pref = oc.get_preferred_model()
    model_id = pref.get("id") if pref else None
    provider = pref.get("providerID") if pref else None
    variant = pref.get("variant") if pref else None
    model_arg = {"id": model_id, "providerID": provider} if model_id else None
    if model_arg and variant:
        model_arg["variant"] = variant
    try:
        oc.send_prompt(sid, text, model=model_arg, variant=variant)
    except Exception as e:
        log.error("send_prompt failed: %s", e)
        mark_failed(f"Failed to send prompt: {e}")
        return False
    return True

def mark_completed():
    global task_state, completion_notified
    if task_state == "idle" or completion_notified:
        return
    aid = allowed_id()
    elapsed = int(time.time() - task_start_time) if task_start_time else 0
    if NOTIFY_ON_COMPLETE and aid:
        try:
            sess = oc.get_session(oc.get_active_session_id())
            model = oc.get_current_model(sess)[0] or "?"
            cost = sess.get("cost", 0)
            notify(aid, f"Task #{current_task_id} finished ({elapsed}s).\nModel: {model}\nCost: ${cost:.4f}\nOutcome: {sess.get('outcome','?')}")
        except Exception as e:
            notify(aid, f"Task #{current_task_id} finished ({elapsed}s).")
    completion_notified = True
    task_state = "idle"
    process_queue()

def mark_failed(reason=""):
    global task_state, completion_notified
    if task_state == "idle" or completion_notified:
        return
    aid = allowed_id()
    if NOTIFY_ON_FAIL and aid:
        try:
            sess = oc.get_session(oc.get_active_session_id())
            notify(aid, f"Task #{current_task_id} failed.\n{reason}\nOutcome: {sess.get('outcome','?')}")
        except Exception:
            notify(aid, f"Task #{current_task_id} failed.\n{reason}")
    completion_notified = True
    task_state = "idle"
    process_queue()

def mark_interrupted():
    global task_state, completion_notified
    if task_state == "idle": return
    aid = allowed_id()
    if aid: notify(aid, f"Task #{current_task_id} interrupted.")
    completion_notified = True
    task_state = "idle"

def process_queue():
    task = pop_next_task()
    if task:
        aid = allowed_id()
        if aid: notify(aid, f"Starting queued task: {task['text'][:80]}")
        start_task(task["text"])

# ---------- command handling ----------
last_activity = None
clear_pending_until = 0.0

def handle_message(sender_id, text):
    global last_activity, clear_pending_until, task_state
    text = (text or "").strip()
    if not text: return
    aid = allowed_id()
    if aid is None:
        if text.startswith("/pair "):
            token = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
            if PAIR_TOKEN and token == PAIR_TOKEN:
                st = load_state(); st["paired_chat_id"] = str(sender_id); save_state(st)
                send_chat(sender_id, "Paired successfully. You are now the authorized user.")
        elif PAIR_TOKEN and text == PAIR_TOKEN:
            st = load_state(); st["paired_chat_id"] = str(sender_id); save_state(st)
            send_chat(sender_id, "Paired successfully. You are now the authorized user.")
        return
    if str(sender_id) != str(aid): return
    last_activity = time.time()

    # help
    if text == "/help":
        send_chat(sender_id, "Commands:\n/status\n/tail N\n/sessions\n/session current|new|<id>\n/models [provider]\n/model current|use <id>\n/variants\n/think <level>\n/agents\n/agent <name>\n/project\n/queue\n/tasks\n/clear\n/approve\n/deny\n/stop\n/pair <token>\n/help\nAnything else is sent to OpenCode."); return
    # status
    if text == "/status":
        exists = session_exists()
        sid = oc.get_active_session_id()
        sess = None
        try: sess = oc.get_session(sid) if sid else None
        except Exception: sess = None
        model, provider, variant = oc.get_current_model(sess) if sess else (None, None, None)
        agent = (sess or {}).get("agent", "?")
        project = (sess or {}).get("location", {}).get("directory", "?")
        outcome = (sess or {}).get("outcome", "?")
        ago = f"{int(time.time()-last_activity)}s ago" if last_activity else "n/a"
        elapsed = f"{int(time.time()-task_start_time)}s" if task_start_time and task_state != "idle" else "n/a"
        send_chat(sender_id, f"Session: {TMUX} ({'running' if exists else 'DOWN'})\nOpenCode session: {sid or '-'}\nProject: {project}\nModel: {provider}/{model}\nVariant: {variant}\nAgent: {agent}\nOutcome: {outcome}\nTask state: {task_state} (elapsed {elapsed})\nQueue: {len(get_queue())} pending\nLast activity: {ago}"); return
    # tail
    if text.startswith("/tail"):
        parts = text.split()
        n = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else TAIL_DEFAULT
        n = max(1, min(n, 500))
        send_chat(sender_id, capture(n).strip() or "(no output)"); return
    # sessions
    if text == "/sessions":
        try:
            sessions = oc.list_sessions()
            if not sessions: send_chat(sender_id, "No sessions."); return
            lines = [oc.format_session(s) for s in sessions[:20]]
            send_chat(sender_id, "Sessions:\n" + "\n".join(lines)); return
        except Exception as e:
            send_chat(sender_id, f"Failed to list sessions: {e}"); return
    # session management
    if text.startswith("/session"):
        parts = text.split()
        if len(parts) == 1 or parts[1] == "current":
            sid = oc.get_active_session_id()
            try: sess = oc.get_session(sid) if sid else None
            except Exception: sess = None
            send_chat(sender_id, f"Active session: {sid or '-'}\n" + (f"Title: {sess.get('title')}\nProject: {sess.get('location',{}).get('directory')}" if sess else "No active session.")); return
        if parts[1] == "new":
            title = " ".join(parts[2:]) if len(parts) > 2 else None
            try:
                s = oc.create_session(WORKDIR, title=title)
                sid = s.get("id") if isinstance(s, dict) else None
                if sid:
                    oc.set_active_session_id(sid)
                    send_chat(sender_id, f"Created session: {sid}")
                else:
                    send_chat(sender_id, "Failed to create session.")
            except Exception as e:
                send_chat(sender_id, f"Failed: {e}")
            return
        # /session <id>
        sid = parts[1]
        try:
            s = oc.get_session(sid)
            if s and s.get("id"):
                oc.set_active_session_id(sid)
                send_chat(sender_id, f"Switched to session: {sid}")
            else:
                send_chat(sender_id, "Session not found.")
        except Exception as e:
            send_chat(sender_id, f"Failed: {e}")
        return
    # models
    if text == "/models" or text.startswith("/models "):
        parts = text.split()
        provider_filter = parts[1] if len(parts) > 1 else None
        try:
            models = oc.list_models(WORKDIR)
            if provider_filter:
                models = [m for m in models if m.get("providerID") == provider_filter]
            if not models:
                send_chat(sender_id, "No models found."); return
            # group by provider
            from collections import defaultdict
            grouped = defaultdict(list)
            for m in models:
                grouped[m.get("providerID", "?")].append(m)
            lines = []
            for prov, ms in sorted(grouped.items()):
                lines.append(f"{prov} ({len(ms)}):")
                for m in ms[:8]:
                    lines.append("  " + oc.format_model(m))
                if len(ms) > 8:
                    lines.append(f"  ... +{len(ms)-8} more")
            send_chat(sender_id, "Models:\n" + "\n".join(lines)); return
        except Exception as e:
            send_chat(sender_id, f"Failed to list models: {e}"); return
    # model current / use
    if text == "/model current" or text == "/model":
        sid = oc.get_active_session_id()
        try:
            sess = oc.get_session(sid) if sid else None
            m, p, v = oc.get_current_model(sess)
            send_chat(sender_id, f"Model: {p}/{m}\nVariant: {v}\nAgent: {(sess or {}).get('agent','?')}")
        except Exception as e:
            send_chat(sender_id, f"Failed: {e}")
        return
    if text.startswith("/model use "):
        model_id = text[len("/model use "):].strip()
        # find provider for this model
        provider = None
        try:
            models = oc.list_models(WORKDIR)
            for m in models:
                if m.get("id") == model_id:
                    provider = m.get("providerID"); break
        except Exception: pass
        if not provider:
            send_chat(sender_id, f"Model not found: {model_id}"); return
        sid = oc.get_active_session_id()
        try:
            oc.set_active_session_id(sid)
            sess = oc.get_session(sid)
            variant = (sess or {}).get("model", {}).get("variant") if sess else None
        except Exception:
            variant = None
        try:
            oc.post(f"/api/session/{sid}/model", {"model": {"id": model_id, "providerID": provider, "variant": variant}})
            oc.set_preferred_model(model_id, provider, variant)
            send_chat(sender_id, f"Model set to {provider}/{model_id}")
        except Exception as e:
            send_chat(sender_id, f"Failed: {e}")
        return
    # variants
    if text == "/variants":
        sid = oc.get_active_session_id()
        try:
            sess = oc.get_session(sid) if sid else None
            m, p, v = oc.get_current_model(sess)
            models = oc.list_models(WORKDIR)
            target = next((mm for mm in models if mm.get("id") == m), None)
            variants = [vv.get("id") for vv in (target.get("variants") or [])] if target else []
            send_chat(sender_id, f"Current model: {p}/{m}\nCurrent variant: {v}\nAvailable variants: {', '.join(variants) if variants else 'none'}")
        except Exception as e:
            send_chat(sender_id, f"Failed: {e}")
        return
    # think <level>
    if text.startswith("/think "):
        level = text.split()[1] if len(text.split()) > 1 else ""
        sid = oc.get_active_session_id()
        try:
            sess = oc.get_session(sid)
            m, p, v = oc.get_current_model(sess)
            models = oc.list_models(WORKDIR)
            target = next((mm for mm in models if mm.get("id") == m), None)
            variants = [vv.get("id") for vv in (target.get("variants") or [])] if target else []
            if level not in variants:
                send_chat(sender_id, f"Variant '{level}' not available. Available: {', '.join(variants)}"); return
            oc.post(f"/api/session/{sid}/model", {"model": {"id": m, "providerID": p, "variant": level}})
            # update preferred variant
            pref = oc.get_preferred_model() or {}
            oc.set_preferred_model(m, p, level)
            send_chat(sender_id, f"Thinking level set to: {level}")
        except Exception as e:
            send_chat(sender_id, f"Failed: {e}")
        return
    # agents
    if text == "/agents":
        try:
            agents = oc.list_agents(WORKDIR)
            lines = [oc.format_agent(a) for a in agents]
            send_chat(sender_id, "Agents:\n" + "\n".join(lines))
        except Exception as e:
            send_chat(sender_id, f"Failed: {e}")
        return
    if text.startswith("/agent "):
        name = text.split(maxsplit=1)[1].strip()
        sid = oc.get_active_session_id()
        try:
            oc.post(f"/api/session/{sid}/agent", {"agent": name})
            send_chat(sender_id, f"Agent set to: {name}")
        except Exception as e:
            send_chat(sender_id, f"Failed: {e}")
        return
    # project
    if text == "/project":
        try:
            projects = oc.list_projects()
            lines = [f"{p.get('id','?')} | {p.get('canonical','?')}" for p in projects]
            sid = oc.get_active_session_id()
            sess = oc.get_session(sid) if sid else None
            cur = (sess or {}).get("location", {}).get("directory", "?")
            send_chat(sender_id, f"Current project: {cur}\n\nProjects:\n" + "\n".join(lines))
        except Exception as e:
            send_chat(sender_id, f"Failed: {e}")
        return
    # tasks
    if text == "/tasks":
        q = get_queue()
        if not q: send_chat(sender_id, "No queued tasks."); return
        lines = [f"{t['id']}. {t['text'][:60]}" for t in q]
        send_chat(sender_id, "Queued tasks:\n" + "\n".join(lines)); return
    # queue
    if text == "/queue":
        q = get_queue()
        if not q: send_chat(sender_id, "Queue is empty."); return
        lines = [f"{t['id']}. {t['text'][:60]}" for t in q]
        send_chat(sender_id, "Pending tasks:\n" + "\n".join(lines)); return
    if text == "/clear" or text == "/clear cancel":
        clear_pending_until = 0.0
        send_chat(sender_id, "Cancelled. Queue unchanged."); return
    if text == "/clear confirm":
        if time.time() > clear_pending_until:
            send_chat(sender_id, "Nothing to confirm. Send /clear first."); return
        n = clear_queue()
        clear_pending_until = 0.0
        send_chat(sender_id, f"Cleared {n} queued task(s)."); return
    if text == "/clear":
        n = len(get_queue())
        if n == 0:
            send_chat(sender_id, "Queue is already empty."); return
        clear_pending_until = time.time() + 30
        send_chat(sender_id, f"{n} task(s) queued. Send /clear confirm within 30s to delete them."); return
    # approve
    if text == "/approve":
        sid = oc.get_active_session_id()
        try:
            perms = oc.list_permissions(sid)
            if not perms:
                send_chat(sender_id, "No permission prompt detected."); return
            rid = perms[0].get("id") if isinstance(perms[0], dict) else perms[0]
            oc.reply_permission(sid, rid, "once")
            send_chat(sender_id, "Approved.")
        except Exception as e:
            send_chat(sender_id, f"Failed: {e}")
        return
    # deny
    if text == "/deny":
        sid = oc.get_active_session_id()
        try:
            perms = oc.list_permissions(sid)
            if perms:
                rid = perms[0].get("id") if isinstance(perms[0], dict) else perms[0]
                oc.reply_permission(sid, rid, "reject")
            else:
                oc.interrupt_session(sid)
            send_chat(sender_id, "Denied.")
        except Exception as e:
            oc.interrupt_session(sid)
            send_chat(sender_id, "Denied (interrupted).")
        return
    # stop
    if text == "/stop":
        sid = oc.get_active_session_id()
        try: oc.interrupt_session(sid)
        except Exception: pass
        interrupt_tmux()
        mark_interrupted()
        send_chat(sender_id, "Sent interrupt."); return
    # pair
    if text.startswith("/pair "):
        token = text.split(maxsplit=1)[1].strip() if len(text.split()) > 1 else ""
        if PAIR_TOKEN and token == PAIR_TOKEN:
            st = load_state(); st["paired_chat_id"] = str(sender_id); save_state(st)
            send_chat(sender_id, "Paired successfully.")
        return

    # free-form prompt
    if task_state in ("running", "waiting_permission"):
        pos = enqueue_task(text)
        send_chat(sender_id, f"Queued (position {pos}). /queue to view."); return
    ok = start_task(text)
    if ok:
        send_chat(sender_id, "Sent to OpenCode.")
    else:
        send_chat(sender_id, "Failed to start task.")

# ---------- watcher ----------
last_snapshot = ""; last_sent = ""; last_change = 0.0; initialized = False

def watcher():
    global last_snapshot, last_sent, last_change, initialized, last_activity
    global task_state, permission_notified, completion_notified, task_start_idle_ms, task_start_outcome
    while True:
        try:
            aid = allowed_id()
            sid = oc.get_active_session_id()
            if sid:
                try:
                    sess = oc.get_session(sid)
                    # permission detection
                    try:
                        perms = oc.list_permissions(sid)
                    except Exception:
                        perms = []
                    if perms and task_state == "running":
                        task_state = "waiting_permission"
                        if not permission_notified and aid and NOTIFY_ON_PERMISSION:
                            notify(aid, f"Task #{current_task_id} is waiting for permission.\nUse /approve or /deny.")
                            permission_notified = True
                    # completion detection
                    if task_state == "running":
                        outcome = sess.get("outcome") if isinstance(sess, dict) else None
                        idle = oc.session_idle_ms(sess) if isinstance(sess, dict) else 0
                        outcome_changed = (outcome != task_start_outcome)
                        idle_advanced = (task_start_idle_ms is not None and idle and idle > task_start_idle_ms)
                        log.debug("watcher: state=%s outcome=%s baseline_outcome=%s idle=%s baseline_idle=%s changed=%s advanced=%s",
                                 task_state, outcome, task_start_outcome, idle, task_start_idle_ms, outcome_changed, idle_advanced)
                        if outcome_changed or idle_advanced:
                            if outcome == "failed":
                                mark_failed("OpenCode reported failure.")
                            elif outcome in ("succeeded", None):
                                mark_completed()
                except Exception as e:
                    log.error("watcher session err: %s", e)
            # tmux output forwarding (best-effort)
            try:
                ensure_session()
                snap = capture(CAPTURE_LINES)
                if not initialized:
                    last_snapshot = last_sent = snap; initialized = True
                    last_activity = time.time(); time.sleep(POLL_MS); continue
                if snap != last_snapshot:
                    last_snapshot = snap; last_change = time.time(); last_activity = time.time()
                if last_snapshot != last_sent and (time.time() - last_change) >= DEBOUNCE:
                    diff = "\n".join(last_snapshot.split("\n")[-10:])  # simple tail
                    last_sent = last_snapshot
                    if diff.strip() and aid:
                        pass  # avoid spamming terminal output; notifications handle completion
            except Exception as e:
                log.error("watcher tmux err: %s", e)
        except Exception as e:
            log.error("watcher err: %s", e)
        time.sleep(POLL_MS)

# ---------- telegram polling ----------
def telegram_poll():
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates?timeout=30&offset={offset}"
            with urllib.request.urlopen(url, timeout=40) as r:
                data = json.loads(r.read())
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                if msg.get("text") is not None and msg.get("chat"):
                    handle_message(msg["chat"]["id"], msg["text"])
        except Exception as e:
            log.error("poll err: %s", e); time.sleep(3)

# ---------- http server ----------
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            self._json(200, {"ok": True, "session": session_exists(), "backend": BACKEND,
                            "task_state": task_state, "queue": len(get_queue()),
                            "opencode_session": oc.get_active_session_id()}); return
        if self.path.split("?")[0] == "/webhook" and BACKEND == "whatsapp":
            q = parse_qs(urlparse(self.path).query)
            if q.get("hub.mode", [""])[0] == "subscribe" and q.get("hub.verify_token", [""])[0] == WA_VERIFY:
                self.send_response(200); self.end_headers()
                self.wfile.write(q.get("hub.challenge", [""])[0].encode()); return
            self.send_response(403); self.end_headers(); return
        self.send_response(404); self.end_headers()
    def do_POST(self):
        if self.path.split("?")[0] != "/webhook":
            self.send_response(404); self.end_headers(); return
        n = int(self.headers.get("Content-Length", 0))
        raw_bytes = self.rfile.read(n)
        raw = raw_bytes.decode("utf-8", "replace")
        if BACKEND == "telegram" and TELEGRAM_SECRET:
            if self.headers.get("X-Telegram-Bot-Api-Secret-Token") != TELEGRAM_SECRET:
                self.send_response(403); self.end_headers(); return
        if BACKEND == "whatsapp" and WA_APP_SECRET:
            sig = self.headers.get("X-Hub-Signature-256")
            if not verify_wa_signature(sig, raw_bytes, WA_APP_SECRET):
                self.send_response(403); self.end_headers(); return
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        try: data = json.loads(raw)
        except Exception: return
        if BACKEND == "telegram":
            msg = data.get("message") or {}
            if msg.get("chat") and msg.get("text") is not None:
                handle_message(msg["chat"]["id"], msg["text"])
        else:
            for e in data.get("entry", []):
                for c in e.get("changes", []):
                    for m in (c.get("value", {}).get("messages") or []):
                        if m.get("type") != "text": continue
                        mid = m.get("id")
                        if mid and is_duplicate_message(mid): continue
                        if mid: mark_message_seen(mid)
                        handle_message(m.get("from"), (m.get("text") or {}).get("body"))
    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(b)
    def log_message(self, *a): pass

def watchdog():
    while True:
        try: ensure_session()
        except Exception as e: log.error("watchdog: %s", e)
        time.sleep(10)

def main():
    global task_state
    # ensure opencode serve
    oc.ensure_serve(WORKDIR)
    sid = oc.get_active_session_id()
    if not sid:
        try:
            s = oc.create_session(WORKDIR, title="WhatsApp remote control")
            sid = s.get("id") if isinstance(s, dict) else None
            if sid: oc.set_active_session_id(sid)
        except Exception as e:
            log.error("session create failed: %s", e)
    log.info("bridge start: session=%s backend=%s opencode_session=%s", TMUX, BACKEND, sid)
    create_session()
    if NOTIFY_ON_RESTART and allowed_id():
        notify(allowed_id(), f"Bridge restarted. OpenCode session: {sid}. {len(get_queue())} queued task(s).")
    threading.Thread(target=watcher, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    if BACKEND == "telegram" and TELEGRAM_MODE == "polling":
        threading.Thread(target=telegram_poll, daemon=True).start()
    if get_queue():
        threading.Timer(2.0, process_queue).start()
    httpd = HTTPServer(("0.0.0.0", PORT), Handler)
    log.info("listening on :%s", PORT)
    httpd.serve_forever()

if __name__ == "__main__":
    main()