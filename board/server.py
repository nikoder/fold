#!/usr/bin/env python3
"""fold board v0 — the control plane's web face.

Read-only state comes straight from Coder and incus; every mutation
shells out to foldctl, so the CLI remains the single audited mutation
path (and the board inherits admission control and pause-default for
free).

Binds 127.0.0.1:7070 by default — reach it over your SSH tunnel or via
a Caddy route later. Env: FOLD_BOARD_BIND, FOLD_BOARD_PORT,
FOLD_CODER_URL, FOLD_CODER_TOKEN_FILE, FOLD_MAX_WORKING.
"""
import json
import os
import re
import subprocess
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
FOLDCTL = os.path.join(HERE, "..", "bin", "foldctl")
CODER = os.environ.get("FOLD_CODER_URL", "http://localhost:7081")
TOKEN_FILE = os.path.expanduser(
    os.environ.get("FOLD_CODER_TOKEN_FILE", "~/coder-eval/.api-token-tasks"))
TOKEN = open(TOKEN_FILE).read().strip()
MAX_WORKING = int(os.environ.get("FOLD_MAX_WORKING", "6"))
ALLOWED_VERBS = {"pause", "resume", "stop"}
CREDS = os.path.expanduser(
    os.environ.get("FOLD_CREDS", "~/coder-eval/bellwether.env"))
NTFY_TOPIC = os.environ.get("FOLD_NTFY", "https://ntfy.fold.example/fold")
STATE_DIR = os.path.expanduser(
    os.environ.get("FOLD_STATE_DIR", "~/.local/state/fold"))
HANDS_FILE = os.path.join(STATE_DIR, "hands.json")

_hands_cache = {"t": 0.0}


def _load_hands():
    try:
        return json.load(open(HANDS_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_hands(store):
    os.makedirs(STATE_DIR, exist_ok=True)
    json.dump(store, open(HANDS_FILE, "w"), indent=1)


def _agent_of(title):
    return re.sub(r"^(raise-hand|hand-down):\s*", "", title).strip()


def hands():
    """Raised hands, durably tracked.

    ntfy is the transport (short memory by design); fold owns the state:
    anything the poll sees is merged into a durable log and stays OPEN
    until resolved — by a human here, or by the agent lowering its own
    hand with a `hand-down: <name>` message on the same direct line.
    The board is a READER of the channel, never a relay: agents' line
    to a human must not depend on this process.
    """
    now = time.time()
    store = _load_hands()
    if now - _hands_cache["t"] >= 15:
        _hands_cache["t"] = now
        try:
            tok = re.search(r"FOLD_BOARD_NTFY_TOKEN=(.*)",
                            open(CREDS).read()).group(1).strip()
            req = urllib.request.Request(
                f"{NTFY_TOPIC}/json?poll=1&since=72h",
                headers={"Authorization": f"Bearer {tok}"})
            with urllib.request.urlopen(req, timeout=15) as r:
                msgs = [json.loads(l)
                        for l in r.read().decode().splitlines() if l]
        except Exception:
            msgs = []
        dirty = False
        for m in sorted(msgs, key=lambda m: m.get("time", 0)):
            if m.get("event") != "message":
                continue
            title = m.get("title", "")
            if title.startswith("raise-hand") and m["id"] not in store:
                store[m["id"]] = {"id": m["id"], "time": m["time"],
                                  "title": title,
                                  "message": m.get("message", ""),
                                  "resolved": None}
                dirty = True
            elif title.startswith("hand-down"):
                agent = _agent_of(title)
                for h in store.values():
                    if (h["resolved"] is None and h["time"] <= m["time"]
                            and _agent_of(h["title"]) == agent):
                        h["resolved"] = {"by": "agent", "t": m["time"],
                                         "note": m.get("message", "")}
                        dirty = True
        if dirty:
            _save_hands(store)
    return sorted((h for h in store.values() if h["resolved"] is None),
                  key=lambda h: h["time"])


def resolve_hand(hand_id):
    store = _load_hands()
    if hand_id in store and store[hand_id]["resolved"] is None:
        store[hand_id]["resolved"] = {"by": "human", "t": int(time.time())}
        _save_hands(store)


def coder(path):
    req = urllib.request.Request(
        f"{CODER}{path}", headers={"Coder-Session-Token": TOKEN})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def incus_instances():
    r = subprocess.run(
        ["incus", "query", "/1.0/instances?recursion=2"],
        capture_output=True, text=True)
    if r.returncode:
        return []
    out = []
    for i in json.loads(r.stdout):
        if not i["name"].startswith("coder-"):
            continue
        mem = ((i.get("state") or {}).get("memory") or {}).get("usage", 0)
        out.append({"name": i["name"], "status": i["status"],
                    "mem_mib": round(mem / 2**20)})
    return out


def state():
    d = coder("/api/experimental/tasks?owner=me")
    tasks = d if isinstance(d, list) else d.get("tasks", [])
    slim = [{
        "name": t["name"],
        "workspace": t["workspace_name"],
        "prompt": (t.get("initial_prompt") or t.get("prompt") or
                   t.get("display_name") or "")[:140],
        "ws_status": t["workspace_status"],
        "agent": t.get("workspace_agent_lifecycle"),
        "task_state": (t.get("current_state") or {}).get("state"),
        "updated": t.get("updated_at", ""),
    } for t in tasks]
    avail = 0.0
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            avail = round(int(line.split()[1]) / 2**20, 1)
    working = sum(1 for t in slim if t["ws_status"] == "running")
    return {"tasks": slim, "fleet": incus_instances(), "hands": hands(),
            "host": {"avail_gib": avail, "max_working": MAX_WORKING,
                     "working": working}}


def instance_ip(ws):
    """eth0 IPv4 of the workspace's incus instance (host reaches incusbr0)."""
    for i in json.loads(subprocess.run(
            ["incus", "query", "/1.0/instances?recursion=2"],
            capture_output=True, text=True).stdout or "[]"):
        if i["name"].startswith("coder-") and \
                i["name"].endswith(f"-{ws.lower()}"):
            eth0 = (((i.get("state") or {}).get("network") or {})
                    .get("eth0") or {})
            for a in eth0.get("addresses", []):
                if a["family"] == "inet":
                    return a["address"]
    return None


def agentapi(ws, path, body=None):
    """Proxy to the agentapi inside the workspace (port 3284)."""
    ip = instance_ip(ws)
    if not ip:
        return {"error": f"no running incus instance for {ws}"}
    req = urllib.request.Request(
        f"http://{ip}:3284{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except Exception as e:
        return {"error": str(e)}


def foldctl(*args):
    r = subprocess.run([FOLDCTL, *args], capture_output=True, text=True,
                       timeout=600)
    return {"ok": r.returncode == 0,
            "out": (r.stdout + r.stderr).strip()[-800:]}


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._send(200, state())
        elif self.path == "/api/events":
            # SSE: push full state whenever it changes (3s cadence).
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            last = None
            try:
                while True:
                    s = json.dumps(state())
                    if s != last:
                        self.wfile.write(f"data: {s}\n\n".encode())
                        self.wfile.flush()
                        last = s
                    time.sleep(3)
            except (BrokenPipeError, ConnectionResetError):
                return
        else:
            m = re.fullmatch(r"/api/task/([\w-]+)/messages", self.path)
            if m:
                return self._send(200, agentapi(m.group(1), "/messages"))
            self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        if self.path == "/api/dispatch":
            tpl, prompt = body.get("template", ""), body.get("prompt", "")
            if not tpl or not prompt:
                return self._send(400, {"error": "template and prompt required"})
            return self._send(200, foldctl("dispatch", tpl, prompt))
        if self.path == "/api/resolve":
            if not body.get("id"):
                return self._send(400, {"error": "id required"})
            resolve_hand(body["id"])
            return self._send(200, {"ok": True})
        if self.path == "/api/action":
            verb, ws = body.get("verb", ""), body.get("workspace", "")
            if verb not in ALLOWED_VERBS or not ws:
                return self._send(400, {"error": "verb must be one of "
                                        f"{sorted(ALLOWED_VERBS)} + workspace"})
            return self._send(200, foldctl(verb, ws))
        m = re.fullmatch(r"/api/task/([\w-]+)/message", self.path)
        if m:
            content = body.get("content", "").strip()
            if not content:
                return self._send(400, {"error": "content required"})
            return self._send(200, agentapi(
                m.group(1), "/message", {"content": content, "type": "user"}))
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    bind = os.environ.get("FOLD_BOARD_BIND", "127.0.0.1")
    port = int(os.environ.get("FOLD_BOARD_PORT", "7070"))
    print(f"fold board on http://{bind}:{port}")
    ThreadingHTTPServer((bind, port), H).serve_forever()
