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
import subprocess
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
    return {"tasks": slim, "fleet": incus_instances(),
            "host": {"avail_gib": avail, "max_working": MAX_WORKING,
                     "working": working}}


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
        else:
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
        if self.path == "/api/action":
            verb, ws = body.get("verb", ""), body.get("workspace", "")
            if verb not in ALLOWED_VERBS or not ws:
                return self._send(400, {"error": "verb must be one of "
                                        f"{sorted(ALLOWED_VERBS)} + workspace"})
            return self._send(200, foldctl(verb, ws))
        self._send(404, {"error": "not found"})

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    bind = os.environ.get("FOLD_BOARD_BIND", "127.0.0.1")
    port = int(os.environ.get("FOLD_BOARD_PORT", "7070"))
    print(f"fold board on http://{bind}:{port}")
    ThreadingHTTPServer((bind, port), H).serve_forever()
