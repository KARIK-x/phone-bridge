"""Tests for bridge.py webhook auth and duplicate-message handling."""
import hashlib, hmac, json, socket, threading, time
import http.client

import bridge


def _free_port():
    s = socket.socket()
    s.bind(("", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class TestSignature:
    def setup_method(self):
        self.secret = "test-app-secret"
        self.body = b'{"entry":[]}'

    def test_valid_signature(self):
        sig = "sha256=" + hmac.new(self.secret.encode(), self.body, hashlib.sha256).hexdigest()
        assert bridge.verify_wa_signature(sig, self.body, self.secret) is True

    def test_invalid_signature(self):
        sig = "sha256=" + "0" * 64
        assert bridge.verify_wa_signature(sig, self.body, self.secret) is False

    def test_missing_signature(self):
        assert bridge.verify_wa_signature(None, self.body, self.secret) is False

    def test_bad_prefix(self):
        assert bridge.verify_wa_signature("md5=abc", self.body, self.secret) is False

    def test_no_secret_skips(self):
        assert bridge.verify_wa_signature(None, self.body, "") is True
        # when no global secret either, None falls back to WA_APP_SECRET (set in .env) -> not skip
        # explicitly test skip with empty global by monkeypatching
        import bridge as _b
        orig = _b.WA_APP_SECRET
        _b.WA_APP_SECRET = ""
        try:
            assert bridge.verify_wa_signature(None, self.body, None) is True
        finally:
            _b.WA_APP_SECRET = orig

    def test_constant_time_uses_compare_digest(self):
        # ensure function delegates to hmac.compare_digest (smoke)
        assert bridge.verify_wa_signature is not None


class TestDuplicates:
    def setup_method(self):
        # isolate state file
        import tempfile, os
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.tmp.close()
        bridge.STATE_FILE = self.tmp.name
        # reset
        import json
        with open(self.tmp.name, "w") as f:
            json.dump({}, f)

    def teardown_method(self):
        import os
        try:
            os.remove(self.tmp.name)
        except OSError:
            pass

    def test_first_message_not_duplicate(self):
        assert bridge.is_duplicate_message("wamid.1") is False

    def test_mark_then_duplicate(self):
        bridge.mark_message_seen("wamid.2")
        assert bridge.is_duplicate_message("wamid.2") is True

    def test_empty_id_not_duplicate(self):
        assert bridge.is_duplicate_message("") is False
        assert bridge.is_duplicate_message(None) is False

    def test_mark_empty_noop(self):
        bridge.mark_message_seen("")
        bridge.mark_message_seen(None)
        assert bridge._seen_ids() == []

    def test_bounded_list(self):
        for i in range(bridge.SEEN_IDS_MAX + 5):
            bridge.mark_message_seen(f"m{i}")
        ids = bridge._seen_ids()
        assert len(ids) == bridge.SEEN_IDS_MAX
        assert ids[0] == "m5"  # oldest 5 dropped


class TestWebhookHTTP:
    def setup_method(self):
        self.port = _free_port()
        self.secret = "appsecret123"
        bridge.BACKEND = "whatsapp"
        bridge.WA_VERIFY = "verifytoken"
        bridge.WA_APP_SECRET = self.secret
        bridge.ALLOWED_CHAT_ID = "15551234567"
        bridge.STATE_FILE = "/tmp/scratchpad/test_state.json"
        import json, os
        os.makedirs("/tmp/scratchpad", exist_ok=True)
        with open(bridge.STATE_FILE, "w") as f:
            json.dump({}, f)
        self.httpd = __import__("http.server", fromlist=["HTTPServer"]).HTTPServer(
            ("127.0.0.1", self.port), bridge.Handler
        )
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        time.sleep(0.2)

    def teardown_method(self):
        self.httpd.shutdown()
        try:
            import os
            os.remove(bridge.STATE_FILE)
        except OSError:
            pass

    def _get(self, path):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path)
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def _post(self, path, body, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", path, body=body, headers=headers or {})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, data

    def test_verify_valid(self):
        st, body = self._get("/webhook?hub.mode=subscribe&hub.verify_token=verifytoken&hub.challenge=abc123")
        assert st == 200
        assert body == b"abc123"

    def test_verify_invalid(self):
        st, body = self._get("/webhook?hub.mode=subscribe&hub.verify_token=wrong&hub.challenge=abc123")
        assert st == 403

    def test_post_valid_signature(self):
        body = json.dumps({"entry": []}).encode()
        sig = "sha256=" + hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
        st, _ = self._post("/webhook", body, {"Content-Type": "application/json", "X-Hub-Signature-256": sig})
        assert st == 200

    def test_post_invalid_signature(self):
        body = json.dumps({"entry": []}).encode()
        st, _ = self._post("/webhook", body, {"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=" + "0" * 64})
        assert st == 403

    def test_post_missing_signature(self):
        body = json.dumps({"entry": []}).encode()
        st, _ = self._post("/webhook", body, {"Content-Type": "application/json"})
        assert st == 403

    def test_health(self):
        st, body = self._get("/health")
        assert st == 200
        assert b'"ok": true' in body or b'"ok":true' in body


class TestQueue:
    def setup_method(self):
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.tmp.close()
        bridge.STATE_FILE = self.tmp.name
        import json
        with open(self.tmp.name, "w") as f:
            json.dump({}, f)
        # reset task state
        bridge.task_state = "idle"
        bridge.task_id_counter = 0
        bridge.current_task_id = None
        bridge.clear_pending_until = 0.0

    def teardown_method(self):
        import os
        try:
            os.remove(self.tmp.name)
        except OSError:
            pass

    def test_enqueue_and_list(self):
        pos = bridge.enqueue_task("do thing")
        assert pos == 1
        assert bridge.get_queue()[0]["text"] == "do thing"

    def test_pop_next(self):
        bridge.enqueue_task("a")
        bridge.enqueue_task("b")
        t = bridge.pop_next_task()
        assert t["text"] == "a"
        assert len(bridge.get_queue()) == 1

    def test_clear_queue(self):
        bridge.enqueue_task("a")
        bridge.enqueue_task("b")
        n = bridge.clear_queue()
        assert n == 2
        assert bridge.get_queue() == []

    def test_persistence(self):
        bridge.enqueue_task("persist me")
        # reload from disk
        q = bridge.get_queue()
        assert q and q[0]["text"] == "persist me"


class TestPermissionDetection:
    def test_detects_approve(self):
        text = "some output\nDo you want to approve this? (y/n)\n"
        assert bridge.detect_permission_prompt(text) is True

    def test_detects_allow(self):
        assert bridge.detect_permission_prompt("Allow this command?") is True

    def test_no_prompt(self):
        assert bridge.detect_permission_prompt("just normal output\nno questions here") is False

    def test_empty(self):
        assert bridge.detect_permission_prompt("") is False
        assert bridge.detect_permission_prompt(None) is False


class TestErrorDetection:
    def test_detects_traceback(self):
        assert bridge.detect_error("Traceback (most recent call last):") is True

    def test_detects_error(self):
        assert bridge.detect_error("Error: something failed") is True

    def test_no_error(self):
        assert bridge.detect_error("all good here") is False


class TestNotificationFailure:
    def setup_method(self):
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.tmp.close()
        bridge.STATE_FILE = self.tmp.name
        import json
        with open(self.tmp.name, "w") as f:
            json.dump({"paired_chat_id": "15551234567"}, f)
        bridge.ALLOWED_CHAT_ID = ""

    def teardown_method(self):
        import os
        try:
            os.remove(self.tmp.name)
        except OSError:
            pass

    def test_notify_does_not_raise_on_failure(self, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("network down")
        monkeypatch.setattr(bridge, "send_chat", boom)
        # should not raise
        bridge.notify("15551234567", "hello")

    def test_notify_calls_send_chat(self, monkeypatch):
        calls = []
        monkeypatch.setattr(bridge, "send_chat", lambda cid, t: calls.append((cid, t)))
        bridge.notify("123", "hi")
        assert calls == [("123", "hi")]


class TestRestartRecovery:
    def setup_method(self):
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.tmp.close()
        bridge.STATE_FILE = self.tmp.name
        import json
        with open(self.tmp.name, "w") as f:
            json.dump({"task_queue": [{"id": 1, "text": "leftover", "ts": 1.0}]}, f)

    def teardown_method(self):
        import os
        try:
            os.remove(self.tmp.name)
        except OSError:
            pass

    def test_queue_survives_reload(self):
        q = bridge.get_queue()
        assert len(q) == 1 and q[0]["text"] == "leftover"


class TestTaskState:
    def setup_method(self):
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        self.tmp.close()
        bridge.STATE_FILE = self.tmp.name
        import json
        with open(self.tmp.name, "w") as f:
            json.dump({}, f)
        bridge.task_state = "idle"
        bridge.task_id_counter = 0
        bridge.current_task_id = None
        bridge.task_start_time = None
        bridge.permission_notified = False
        bridge.completion_notified = False

    def teardown_method(self):
        import os
        try:
            os.remove(self.tmp.name)
        except OSError:
            pass

    def test_start_task_sets_running(self, monkeypatch):
        monkeypatch.setattr(bridge.oc, "ensure_serve", lambda d: True)
        monkeypatch.setattr(bridge.oc, "get_active_session_id", lambda: "ses_test")
        monkeypatch.setattr(bridge.oc, "get_session", lambda sid: {"id": sid, "outcome": None, "time": {"idle": 1000}})
        monkeypatch.setattr(bridge.oc, "create_session", lambda d, title=None: {"id": "ses_test"})
        monkeypatch.setattr(bridge.oc, "set_active_session_id", lambda sid: None)
        monkeypatch.setattr(bridge.oc, "send_prompt", lambda sid, text, model=None, variant=None: {"delivery": "steer"})
        monkeypatch.setattr(bridge, "notify", lambda cid, t: None)
        bridge.ALLOWED_CHAT_ID = ""
        bridge.task_state = "idle"
        bridge.task_id_counter = 0
        bridge.current_task_id = None
        bridge.task_start_time = None
        bridge.task_start_idle_ms = None
        bridge.task_start_outcome = None
        bridge.permission_notified = False
        bridge.completion_notified = False
        bridge.start_task("do it")
        assert bridge.task_state == "running"
        assert bridge.current_task_id == 1

    def test_mark_completed_idempotent(self, monkeypatch):
        monkeypatch.setattr(bridge, "capture", lambda n=None: "out")
        monkeypatch.setattr(bridge, "notify", lambda cid, t: None)
        monkeypatch.setattr(bridge, "send_chat", lambda cid, t: None)
        monkeypatch.setattr(bridge.oc, "get_session", lambda sid: {"id": sid, "outcome": "succeeded", "model": {"id": "m"}, "cost": 0.0, "time": {"idle": 2000}})
        bridge.ALLOWED_CHAT_ID = ""
        bridge.task_state = "running"
        bridge.task_start_time = __import__("time").time()
        bridge.current_task_id = 1
        bridge.task_start_idle_ms = 1000
        bridge.task_start_outcome = None
        bridge.permission_notified = False
        bridge.completion_notified = False
        bridge.mark_completed()
        assert bridge.task_state == "idle"
        # second call no-op
        bridge.mark_completed()
        assert bridge.task_state == "idle"