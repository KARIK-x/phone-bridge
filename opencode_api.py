"""OpenCode Server API client (localhost only)."""
import base64, json, os, secrets, subprocess, time, urllib.error, urllib.parse, urllib.request
import logging
log = logging.getLogger("bridge")

SERVE_HOST = "127.0.0.1"
SERVE_PORT = int(os.environ.get("OPENCODE_SERVE_PORT", "4096"))
STATE_FILE = os.environ.get("BRIDGE_STATE_FILE", os.path.join(os.path.dirname(__file__), "state.json"))


def _load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


def _password():
    st = _load_state()
    if "opencode_serve_password" in st:
        return st["opencode_serve_password"]
    pw = secrets.token_urlsafe(24)
    st["opencode_serve_password"] = pw
    _save_state(st)
    return pw


def _auth_headers():
    pw = _password()
    creds = base64.b64encode(b"opencode:" + pw.encode()).decode()
    return {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}


def _url(path):
    return f"http://{SERVE_HOST}:{SERVE_PORT}{path}"


def _get(path, params=None, timeout=20):
    url = _url(path)
    if params:
        qs = urllib.parse.urlencode(params)
        url = url + "?" + qs
    req = urllib.request.Request(url, headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def get(path, params=None, timeout=20):
    return _get(path, params=params, timeout=timeout)


def _post(path, body, timeout=60):
    data = json.dumps(body).encode()
    req = urllib.request.Request(_url(path), data=data, headers=_auth_headers())
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def post(path, body, timeout=60):
    return _post(path, body, timeout=timeout)


def _is_serve_up():
    try:
        _get("/api/session", timeout=3)
        return True
    except Exception:
        return False


def ensure_serve(directory):
    """Start opencode serve if not running. Returns True on success."""
    if _is_serve_up():
        return True
    pw = _password()
    env = dict(os.environ)
    env["OPENCODE_PASSWORD"] = pw
    env.setdefault("OPENCODE_LOG_LEVEL", "warn")
    try:
        subprocess.Popen(
            ["opencode", "serve", "--port", str(SERVE_PORT), "--hostname", SERVE_HOST],
            cwd=directory or os.path.expanduser("~"),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        log.error("failed to start opencode serve: %s", e)
        return False
    for _ in range(30):
        time.sleep(0.5)
        if _is_serve_up():
            log.info("opencode serve ready on :%s", SERVE_PORT)
            return True
    log.error("opencode serve did not become ready")
    return False


def stop_serve():
    # best-effort; serve is a child of the bridge process group? start_new_session=True so no.
    pass


def list_sessions():
    r = _get("/api/session")
    return r.get("data", r) if isinstance(r, dict) else r


def get_session(session_id):
    r = _get(f"/api/session/{session_id}")
    return r.get("data", r) if isinstance(r, dict) else r


def create_session(directory, title=None):
    body = {"directory": directory}
    if title:
        body["title"] = title
    r = _post("/api/session", body)
    return r.get("data", r) if isinstance(r, dict) else r


def send_prompt(session_id, text, model=None, variant=None):
    body = {"text": text}
    if model:
        m = {"id": model}
        if variant:
            m["variant"] = variant
        # need providerID; if model is just id, look it up
        if isinstance(model, dict):
            body["model"] = model
        else:
            # model is an id string; find provider
            prov = None
            try:
                for mm in list_models(os.environ.get("OPENCODE_WORKDIR") or os.path.expanduser("~")):
                    if mm.get("id") == model:
                        prov = mm.get("providerID")
                        break
            except Exception:
                pass
            if prov:
                mm = {"id": model, "providerID": prov}
                if variant:
                    mm["variant"] = variant
                body["model"] = mm
    return _post(f"/api/session/{session_id}/prompt", body)


def get_preferred_model():
    st = _load_state()
    return st.get("preferred_model")


def set_preferred_model(model_id, provider_id, variant=None):
    st = _load_state()
    st["preferred_model"] = {"id": model_id, "providerID": provider_id, "variant": variant}
    _save_state(st)


def pick_default_model(directory):
    try:
        models = list_models(directory)
    except Exception:
        return None
    # prefer free models from opencode/opencode-go providers
    free = [m for m in models if "free" in (m.get("id") or "").lower()
            and m.get("providerID") in ("opencode", "opencode-go")]
    if free:
        m = free[0]
        return m.get("id"), m.get("providerID")
    # fallback: first model from opencode-go or opencode
    for prov in ("opencode-go", "opencode"):
        ms = [m for m in models if m.get("providerID") == prov]
        if ms:
            return ms[0].get("id"), prov
    if models:
        return models[0].get("id"), models[0].get("providerID")
    return None


def interrupt_session(session_id):
    try:
        return _post(f"/api/session/{session_id}/interrupt", {})
    except Exception as e:
        log.warning("interrupt failed: %s", e)
        return None


def list_models(directory):
    r = _get("/api/model", {"directory": directory})
    return r.get("data", r) if isinstance(r, dict) else r


def list_agents(directory):
    r = _get("/api/agent", {"directory": directory})
    return r.get("data", r) if isinstance(r, dict) else r


def list_projects():
    r = _get("/api/project")
    return r if isinstance(r, list) else r.get("data", r)


def list_permissions(session_id):
    try:
        r = _get(f"/api/session/{session_id}/permission")
        return r.get("data", r) if isinstance(r, dict) else r
    except Exception:
        return []


def reply_permission(session_id, request_id, reply):
    return _post(f"/api/session/{session_id}/permission/{request_id}/reply", {"reply": reply})


def get_active_session_id():
    st = _load_state()
    return st.get("active_session_id")


def set_active_session_id(sid):
    st = _load_state()
    st["active_session_id"] = sid
    _save_state(st)


def get_current_model(session):
    m = (session or {}).get("model") or {}
    return m.get("id"), m.get("providerID"), m.get("variant")


def session_outcome(session):
    return (session or {}).get("outcome")


def session_idle_ms(session):
    t = (session or {}).get("time") or {}
    return t.get("idle") or t.get("updated") or 0


def format_session(s):
    sid = s.get("id", "?")
    title = s.get("title") or "(untitled)"
    outcome = s.get("outcome") or "unknown"
    model = (s.get("model") or {}).get("id", "?")
    return f"{sid} | {title} | {outcome} | {model}"


def format_model(m):
    mid = m.get("id", "?")
    variants = [v.get("id") for v in (m.get("variants") or [])]
    vstr = ",".join(variants) if variants else "-"
    return f"{mid} (variants: {vstr})"


def format_agent(a):
    return f"{a.get('id','?')} - {a.get('name','?')} ({a.get('mode','?')})"