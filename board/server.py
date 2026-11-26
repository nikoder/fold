#!/usr/bin/env python3
"""fold board v0 — the control plane's web face.

Read-only state comes straight from incus and the task registry; every
mutation shells out to foldctl, so the CLI remains the single audited
mutation path (and the board inherits admission control and
pause-default for free).

Human access is the reverse-proxy front door: the proxy authenticates
the human (any gate works — ours is a WebAuthn key-tap) and injects the
X-Fold-Human secret header. Loopback and the agent bearer plane also
work. Env: FOLD_BOARD_BIND, FOLD_BOARD_PORT, FOLD_MAX_WORKING.
"""
import hashlib
import hmac
import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
FOLDCTL = os.path.join(HERE, "..", "bin", "foldctl")
MAX_WORKING = int(os.environ.get("FOLD_MAX_WORKING", "6"))
ALLOWED_VERBS = {"pause", "resume", "stop", "start"}
CREDS = os.path.expanduser(
    os.environ.get("FOLD_CREDS", "~/.config/fold/creds.env"))
STATE_DIR = os.path.expanduser(
    os.environ.get("FOLD_STATE_DIR", "~/.local/state/fold"))


def _fold_env(name, default=None):
    """Deployment specifics live in STATE_DIR/fold.env (KEY=VALUE),
    outside the repo; process env overrides. Same contract as foldctl."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        for line in open(os.path.join(STATE_DIR, "fold.env")):
            line = line.strip()
            if line.startswith(name + "="):
                return line.partition("=")[2].strip()
    except FileNotFoundError:
        pass
    return default


NTFY_TOPIC = _fold_env("FOLD_NTFY")  # full topic URL; unset = no hands feed


def _creds_repo_guard():
    """Creds inside a checkout of a repo agents work on = keys shipped
    into every agent workspace: refuse to start (same rule as foldctl,
    which also re-checks on every dispatch the board shells out).
    Inside any other git repo: warn loudly."""
    if not os.path.exists(CREDS):
        return
    d = os.path.dirname(os.path.abspath(CREDS))
    while not os.path.exists(os.path.join(d, ".git")):
        parent = os.path.dirname(d)
        if parent == d:
            return
        d = parent
    assoc = {v for v in (_fold_env("FOLD_WORK_REPO"),
                         _fold_env("FOLD_SHEPHERD_REPO")) if v}
    try:
        r = subprocess.run(["git", "-C", d, "remote", "-v"],
                           capture_output=True, text=True)
        remotes = r.stdout if r.returncode == 0 else ""
    except OSError:
        remotes = ""  # no git binary: fall through to the loud warning
    for a in assoc:
        if re.search(rf"[:/]{re.escape(a)}(\.git)?(\s|$)", remotes):
            raise SystemExit(
                f"REFUSING to start: credentials file {CREDS} sits inside "
                f"a checkout of {a} ({d}) — a repo fold agents work on. "
                "Move it somewhere unversioned and point FOLD_CREDS at it.")
    print(f"WARNING: credentials file {CREDS} sits inside a git "
          f"repository ({d}). Move it somewhere unversioned and "
          "point FOLD_CREDS at it.", flush=True)


_creds_repo_guard()
HANDS_FILE = os.path.join(STATE_DIR, "hands.json")
CTX_FILE = os.path.join(STATE_DIR, "ctx.json")

# Two planes: loopback (the human's tunnel) has every verb. Anything else
# — shepherd sessions on the incus bridge — must present the agent-plane
# bearer token and gets only these paths: it can create work and talk,
# never pause, stop, resolve, or read the human UI.
AGENT_PLANE_PATHS = {
    ("POST", "/api/dispatch"), ("GET", "/api/state"),
}
AGENT_PLANE_RES = [
    ("POST", re.compile(r"/api/task/[\w-]+/message$")),
    ("GET", re.compile(r"/api/task/[\w-]+/messages$")),
]


def board_token():
    m = re.search(r"FOLD_BOARD_TOKEN=(.*)", open(CREDS).read())
    return m.group(1).strip() if m else None


def webhook_secret():
    m = re.search(r"FOLD_WEBHOOK_SECRET=(.*)", open(CREDS).read())
    return m.group(1).strip() if m else None


def task_ctx():
    try:
        return json.load(open(CTX_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

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
    if NTFY_TOPIC and now - _hands_cache["t"] >= 15:
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
                tags = m.get("tags") or []
                store[m["id"]] = {"id": m["id"], "time": m["time"],
                                  "title": title,
                                  "message": m.get("message", ""),
                                  "workspace": tags[0] if tags else None,
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
                        _notify_forwarded(h)
        if dirty:
            _save_hands(store)
        if _forward_hands(store):
            _save_hands(store)
    return sorted((h for h in store.values() if h["resolved"] is None),
                  key=lambda h: h["time"])


def _forward_hands(store):
    """Routing stage C: bring a task's open hand to its shepherd.

    Forwarding never replaces the direct line — the human's push already
    fired and the hand stays open on the board until resolved (by human,
    or by the agent's hand-down after the shepherd's answer). We forward
    only when the ctx session is at rest, retrying on later polls.
    """
    ctx = task_ctx()
    dirty = False
    for h in store.values():
        if h["resolved"] or h.get("forwarded") or not h.get("workspace"):
            continue
        session = ctx.get(h["workspace"])
        if not session:
            continue
        # Delivery is not processing: agentapi types the note into the
        # TUI, and a session that is quietly thinking mid-turn reads
        # "stable" to agentapi's screen-based heuristic — the typed note
        # is swallowed without a trace (observed on the first
        # language-menu hand). So a send only becomes "forwarded" once
        # the note demonstrably started a turn: forward_pending first,
        # promoted when the session turns running, cleared for a retry
        # when it stayed stable. A fast turn can double-send; the note
        # is idempotent and the hand stays open either way.
        pend = h.get("forward_pending")
        if pend:
            if time.time() - pend["t"] < 4:
                continue
            # Promotion needs ground truth, not the status flip (the
            # same keystroke-redraw false positive as every other typed
            # delivery): the forward counts only once the ctx session's
            # own db holds the note.
            if delivered_to_db(pend["to"], pend.get("probe", ""), tries=3):
                h["forwarded"] = {"to": pend["to"], "t": int(time.time())}
            h.pop("forward_pending", None)
            dirty = True
            continue
        try:
            ip = instance_ip(session)
            if not ip:
                continue
            req = urllib.request.Request(f"http://{ip}:3284/status",
                                          headers={"Host": "localhost"})
            with urllib.request.urlopen(req, timeout=5) as r:
                if json.load(r)["status"] != "stable":
                    continue
        except Exception:
            continue
        agent = re.sub(r"^raise-hand:\s*", "", h["title"]).strip()
        note = (f"[fold] raise-hand from {agent} (workspace "
                f"{h['workspace']}, dispatched under your ctx): "
                f"\"{h['message']}\"\n"
                f"Per your three-tier rule: if this is tier 1 or 2, answer "
                f"the agent via POST $FOLD_BOARD_URL/api/task/"
                f"{h['workspace']}/message and state your evidence; if it "
                f"requires a new decision (tier 3), do not answer — say so "
                f"and leave it for the human. Then rest. The hand stays "
                f"open until resolved either way.")
        r = agentapi(session, "/message", {"content": note, "type": "user"})
        if "error" not in r:
            h["forward_pending"] = {"to": session, "t": int(time.time()),
                                    "probe": note[:80]}
            dirty = True
    return dirty


def _notify_forwarded(h):
    """A forwarded hand's resolution travels the same road back: the
    ctx session answered (or deferred to the human) and may be resting
    on the outcome — observed: a shepherd that kept believing phase 2
    was blocked long after the hand came down. FYI mail, no wake."""
    fwd = (h.get("forwarded") or {}).get("to")
    if not fwd:
        return
    res = h.get("resolved") or {}
    note = (res.get("note") or "").strip()
    enqueue_message(fwd, (
        f"[fold] The hand you were forwarded from "
        f"{_agent_of(h.get('title', ''))} (workspace "
        f"{h.get('workspace', '?')}) has been resolved by "
        f"{res.get('by', '?')}"
        + (f": \"{note[:300]}\"" if note else ".")
        + " FYI — if you were resting on that blockage, it is lifted; "
          "update your notes as needed."))
    print(f"[hands] resolution notice -> {fwd} "
          f"({h.get('workspace')})", flush=True)


def resolve_hand(hand_id):
    store = _load_hands()
    if hand_id in store and store[hand_id]["resolved"] is None:
        store[hand_id]["resolved"] = {"by": "human", "t": int(time.time())}
        _save_hands(store)
        _notify_forwarded(store[hand_id])


def incus_instances():
    r = subprocess.run(
        ["incus", "query", "/1.0/instances?recursion=2"],
        capture_output=True, text=True)
    if r.returncode:
        return []
    out = []
    for i in json.loads(r.stdout):
        if not i["name"].startswith("fold-"):
            continue
        mem = ((i.get("state") or {}).get("memory") or {}).get("usage", 0)
        out.append({"name": i["name"], "status": i["status"],
                    "mem_mib": round(mem / 2**20)})
    return out


def state():
    ctx = task_ctx()
    fleet = incus_instances()
    # A frozen instance is not working (zero CPU, zero inference): it
    # must not hold a working slot.
    frozen_ws = {f["name"][5:] for f in fleet
                 if f["status"] == "Frozen" and f["name"].startswith("fold-")}
    inst_state = {f["name"][5:]: f["status"]
                  for f in fleet if f["name"].startswith("fold-")}
    slim = []
    # Registry identity + live incus/agentapi state.
    try:
        reg = json.load(open(os.path.join(STATE_DIR, "tasks.json")))
    except (FileNotFoundError, json.JSONDecodeError):
        reg = {}
    for name, meta in reg.items():
        st = inst_state.get(name.lower(), "Stopped")
        ws_status = {"Running": "running", "Frozen": "running",
                     "Stopped": "stopped"}.get(st, st.lower())
        task_state = None
        if st == "Running":
            probe = agentapi(name, "/status")
            task_state = ({"running": "working", "stable": "idle"}
                          .get(probe.get("status"), None))
        slim.append({
            "name": name, "ctx": meta.get("ctx"),
            "project": meta.get("project"),
            "model": meta.get("model"), "llm": meta.get("llm"),
            "template": f"native-{meta.get('flavor', 'agent')}",
            "workspace": name,
            "prompt": (meta.get("prompt") or "")[:140],
            "ws_status": ws_status,
            "frozen": name.lower() in frozen_ws,
            "agent": "ready" if st == "Running" else None,
            "task_state": task_state,
            "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                     time.gmtime(meta.get("created", 0))),
        })
    slim.sort(key=lambda t: t.get("updated") or "", reverse=True)
    avail = 0.0
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            avail = round(int(line.split()[1]) / 2**20, 1)
    try:
        tokens = json.load(open(TOKENS_FILE))
    except (FileNotFoundError, json.JSONDecodeError):
        tokens = {}
    for t in slim:
        t["tok"] = tokens.get(t["workspace"])
    # mirrors dispatch admission: awake sessions, not running instances
    working = sum(1 for t in slim if t.get("task_state") == "working")
    total_out = sum(v.get("output", 0) for v in tokens.values())
    try:
        eps = json.load(open(os.path.join(STATE_DIR, "endpoints.json")))
    except (FileNotFoundError, json.JSONDecodeError):
        eps = {}
    ep_summary = {k: v.get("models", []) for k, v in eps.items()
                  if not k.startswith("_")}
    try:
        rr = json.load(open(os.path.join(STATE_DIR, "repos.json")))
    except (FileNotFoundError, json.JSONDecodeError):
        rr = {}
    repo_summary = {k: v.get("repo") for k, v in rr.items()
                    if not k.startswith("_") and isinstance(v, dict)}
    repo_reviews = {k: v.get("review", "bellwether") for k, v in rr.items()
                    if not k.startswith("_") and isinstance(v, dict)}
    try:
        bellwether = json.load(open(os.path.join(STATE_DIR,
                                                 "bellwether.json")))
    except (FileNotFoundError, json.JSONDecodeError):
        bellwether = None
    try:
        reviews = json.load(open(os.path.join(STATE_DIR, "reviews.json")))
    except (FileNotFoundError, json.JSONDecodeError):
        reviews = {}
    return {"tasks": slim, "fleet": fleet, "hands": hands(),
            "bellwether": bellwether, "reviews": reviews,
            "repos": repo_summary, "repo_reviews": repo_reviews,
            "repo_default": rr.get("_default"),
            "endpoints": ep_summary, "llm_default": eps.get("_default", {}),
            "host": {"avail_gib": avail, "max_working": MAX_WORKING,
                     "working": working, "total_output_tokens": total_out}}


TRANSCRIPTS = os.path.join(STATE_DIR, "transcripts")
TOKENS_FILE = os.path.join(STATE_DIR, "tokens.json")


def _instance_for(ws):
    for i in incus_instances():
        if i["name"].endswith(f"-{ws.lower()}"):
            return i["name"]
    return None


def _pull_db(ws, tag="tok"):
    """Pull the workspace's opencode.db (plus WAL) to a throwaway host
    copy; returns its path, or None when there is no instance or no db.

    Tags keep the archiver and on-demand readers off each other's
    copies. Stale sidecars are removed first: replaying a previous
    cycle's WAL over this cycle's db would resurrect old state.
    """
    inst = _instance_for(ws)
    if not inst:
        return None
    tmp = f"/tmp/{tag}-{ws}"
    base = f"{inst}/home/coder/.local/share/opencode/opencode.db"
    ok = False
    for suffix in ("", "-wal", "-shm"):
        dst = f"{tmp}.db{suffix}"
        try:
            os.unlink(dst)
        except FileNotFoundError:
            pass
        r = subprocess.run(["incus", "file", "pull", base + suffix, dst],
                           capture_output=True, timeout=60)
        if not suffix:
            ok = r.returncode == 0
    return f"{tmp}.db" if ok else None


def _db_messages(dbpath):
    """Rebuild the conversation from a pulled opencode.db copy.

    agentapi messages are screen scrapes — a response longer than the
    TUI viewport survives only as its tail. The db holds every part in
    full, so Visit reads here first and keeps the scrape as fallback
    for harnesses without a db adapter.
    """
    import sqlite3 as sq
    try:
        con = sq.connect(dbpath)
        rows = con.execute(
            "SELECT m.id, m.data, p.data FROM message m "
            "LEFT JOIN part p ON p.message_id = m.id "
            "ORDER BY m.rowid, p.rowid").fetchall()
        con.close()
    except Exception:
        return None
    out, last_id = [], None
    for mid, mdata, pdata in rows:
        try:
            m = json.loads(mdata)
            p = json.loads(pdata) if pdata else {}
        except json.JSONDecodeError:
            continue
        if mid != last_id:
            out.append({"role": "user" if m.get("role") == "user"
                        else "agent", "content": ""})
            last_id = mid
        kind, prev = p.get("type"), out[-1]["content"]
        if kind == "text" and p.get("text"):
            out[-1]["content"] = (prev + "\n\n" if prev else "") + p["text"]
        elif kind == "reasoning" and p.get("text"):
            out[-1]["content"] = (prev + "\n" if prev else "") + \
                f"· reasoned ({len(p['text'])} chars) ·"
        elif kind == "tool":
            title = ((p.get("state") or {}).get("title") or "").strip()
            out[-1]["content"] = (prev + "\n" if prev else "") + \
                "⚙ " + p.get("tool", "tool") + (f" — {title}" if title else "")
    out = [e for e in out if e["content"].strip()]
    return out or None


def _harvest_tokens(ws, tokens, dbpath):
    """Sum per-message token usage from a pulled opencode.db copy.

    Attribution without a metering proxy: OpenCode records tokens per
    assistant message; we pull the db (plus WAL) and sum. Totals persist
    here after the workspace stops; the output rate between snapshots is
    the "token intensity" review signal.
    """
    import sqlite3 as sq
    try:
        # writable: these are our throwaway copies, and read-only mode
        # cannot replay the WAL — a fresh session's data lives there
        # entirely until opencode checkpoints
        con = sq.connect(dbpath)
        tin = tout = msgs = peak = last = cread = cwrite = last_t = 0
        for data, tu in con.execute(
                "SELECT data, time_updated FROM message ORDER BY rowid"):
            last_t = max(last_t, int((tu or 0) / 1000))
            try:
                d = json.loads(data)
            except json.JSONDecodeError:
                continue
            tk = d.get("tokens") or {}
            if d.get("role") == "assistant" and tk:
                # summed input is a BILLING figure (context re-sent per
                # turn); the single-turn input is the live context size —
                # peak answers "how big did this task's mind get", last
                # answers "how big is it now". Cache fields flow when the
                # provider reports them (vLLM prefix hits currently don't).
                i, o = tk.get("input", 0), tk.get("output", 0)
                tin += i
                tout += o
                msgs += 1
                if i:
                    # the context at a turn's END includes what the turn
                    # generated: input + output is the true water mark
                    peak = max(peak, i + o)
                    last = i + o
                c = tk.get("cache") or {}
                cread += c.get("read", 0)
                cwrite += c.get("write", 0)
        con.close()
    except Exception:
        return
    now = int(time.time())
    prev = tokens.get(ws)
    rate = 0
    if prev and now > prev["t"] and tout >= prev["output"]:
        rate = round((tout - prev["output"]) * 60 / (now - prev["t"]))
    tokens[ws] = {"input": tin, "output": tout, "msgs": msgs,
                  "peak_ctx": peak, "last_ctx": last,
                  "cache_read": cread, "cache_write": cwrite,
                  "t": now, "out_per_min": rate, "last_t": last_t}


def archive_transcripts():
    """Snapshot every reachable task's agentapi messages every 30s, so
    the transcript survives stop/freeze and Visit works in any state."""
    while True:
        try:
            try:
                tokens = json.load(open(TOKENS_FILE))
            except (FileNotFoundError, json.JSONDecodeError):
                tokens = {}
            dirty = False
            try:
                reg = json.load(open(os.path.join(STATE_DIR, "tasks.json")))
            except (FileNotFoundError, json.JSONDecodeError):
                reg = {}
            running_native = {i["name"][5:] for i in incus_instances()
                              if i["name"].startswith("fold-")
                              and i["status"] == "Running"}
            live = [n for n in reg if n.lower() in running_native]
            for ws in live:
                db = _pull_db(ws)
                if db:
                    _harvest_tokens(ws, tokens, db)
                    dirty = True
                msgs = _db_messages(db) if db else None
                if msgs is None:
                    scraped = agentapi(ws, "/messages")
                    if "error" in scraped:
                        continue
                    msgs = scraped.get("messages", [])
                os.makedirs(TRANSCRIPTS, exist_ok=True)
                json.dump({"archived_at": int(time.time()),
                           "messages": msgs},
                          open(os.path.join(TRANSCRIPTS, f"{ws}.json"), "w"))
            if dirty:
                os.makedirs(STATE_DIR, exist_ok=True)
                json.dump(tokens, open(TOKENS_FILE, "w"))
            # Auto-park after a day of recorded rest (reviewers
            # 2026-10-26; authors 2026-11-24 — the decline path left
            # them with no park actor, since only merges parked them).
            # Both rest live and messageable after their handoff — the
            # conversation happens near the work — then park. Shepherds
            # are resident by design and never swept. foldctl
            # re-verifies db-level quiescence AND an empty outbox at
            # park time, so a late conversation or undelivered mail
            # still blocks the stop. Parking is hibernation: doorbells
            # and findings bounces wake a parked author.
            for ws in live:
                meta = reg.get(ws, {})
                if meta.get("flavor") == "shepherd":
                    continue
                last_t = (tokens.get(ws) or {}).get("last_t")
                if last_t and time.time() - last_t > 24 * 3600:
                    r = foldctl("stop", ws, "--when-quiet", "60")
                    # Conclusion notice, decline-path half (the gate
                    # sends the settled half on a no-findings merge):
                    # "declined with reasons" is prose the machinery
                    # deliberately never reads, so a day of verified
                    # rest is its honest conclusion that nothing more
                    # is coming. FYI mail, no wake — a parked ctx
                    # session reads it at its next start.
                    ctx = meta.get("ctx")
                    if r.get("ok") and ctx and ctx != ws and ctx in reg:
                        enqueue_message(ctx, (
                            f"[fold] Task {ws} "
                            f"({meta.get('project') or 'no project'}), "
                            "dispatched under your context, rested a "
                            "full day and has been parked — its thread "
                            "is concluded. FYI, nothing needs doing "
                            "now: its closing note, PRs and any "
                            "'review findings' ledger issues are on "
                            "the forge for your next planning round."))
                        print(f"[archiver] conclusion notice (idle "
                              f"park): {ws} -> {ctx}", flush=True)
        except Exception:
            pass
        time.sleep(30)


OUTBOX = os.path.join(STATE_DIR, "outbox.json")
_outbox_lock = threading.Lock()


def _outbox(mutate=None):
    with _outbox_lock:
        try:
            box = json.load(open(OUTBOX))
        except (FileNotFoundError, json.JSONDecodeError):
            box = {}
        if mutate is not None:
            box = mutate(box) or box
            os.makedirs(STATE_DIR, exist_ok=True)
            json.dump(box, open(OUTBOX, "w"), indent=1)
        return box


def enqueue_message(ws, content):
    def add(box):
        box.setdefault(ws, []).append(
            {"content": content, "t": int(time.time()), "tries": 0})
    _outbox(add)


def delivered_to_db(ws, content, tries=6):
    """Ground truth for message delivery: the harness db grew a user
    message carrying this text. The screen-status flip lies in both
    directions — keystrokes redraw the TUI into 'running' while the
    typed text can still vanish without a trace (observed: a notice
    that passed the flip check yet never existed in the db, leaving a
    shepherd believing phase 2 was still blocked). The db row is
    written the moment the harness accepts the input, so this is the
    only check that cannot be fooled by rendering."""
    probe = content[:80]
    for _ in range(tries):
        time.sleep(1)
        try:
            db = _pull_db(ws)
            msgs = (_db_messages(db) if db else None) or []
            for m in reversed(msgs):
                if (m.get("role") == "user"
                        and probe in (m.get("content") or "")):
                    return True
        except Exception:
            pass
    return False


def deliver_outbox():
    """A refused message queues instead of vanishing: sessions mid-turn
    (or sleeping IN-turn — observed: an agent chaining nine-minute
    `sleep` tool calls reads busy for half an hour and starves every
    courier) accept nothing, so the human's words wait here and a tight
    3s cadence hunts the brief stable gaps between turns. Delivery uses
    the same processing-verification as direct sends; stopped sessions
    simply hold their queue until they are next started."""
    while True:
        try:
            for ws, msgs in list(_outbox().items()):
                if not msgs:
                    continue
                if agentapi(ws, "/status").get("status") != "stable":
                    continue
                m = msgs[0]
                # Genuine-failure backoff: with db-verified delivery a
                # swallowing TUI now fails honestly instead of
                # false-passing, so after a burst of quick retries the
                # cadence drops to five minutes — the queue stays
                # visible on the board either way.
                if (m.get("tries", 0) >= 8
                        and time.time() - m.get("last_try", 0) < 300):
                    continue
                r = agentapi(ws, "/message",
                             {"content": m["content"], "type": "user"})
                if "error" in r:
                    continue
                delivered = delivered_to_db(ws, m["content"])
                def record(box):
                    q = box.get(ws) or []
                    if q and q[0]["t"] == m["t"]:
                        if delivered:
                            q.pop(0)
                        else:
                            q[0]["tries"] = q[0].get("tries", 0) + 1
                            q[0]["last_try"] = int(time.time())
                    if not q:
                        box.pop(ws, None)
                _outbox(record)
                if delivered:
                    print(f"[outbox] delivered to {ws}", flush=True)
        except Exception:
            pass
        time.sleep(3)


def archived_transcript(ws):
    try:
        return json.load(open(os.path.join(TRANSCRIPTS, f"{ws}.json")))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def instance_ip(ws):
    """eth0 IPv4 of the workspace's incus instance (host reaches incusbr0)."""
    for i in json.loads(subprocess.run(
            ["incus", "query", "/1.0/instances?recursion=2"],
            capture_output=True, text=True).stdout or "[]"):
        if i["name"] == f"fold-{ws.lower()}":
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
        # newer agentapi rejects non-localhost Host headers; we are the
        # trusted proxy, so present the one it expects
        headers={"Content-Type": "application/json", "Host": "localhost"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 500 and path == "/message":
            return {"error": "agent is busy (mid-generation) — "
                    "try again in a moment"}
        return {"error": str(e)}
    except Exception as e:
        return {"error": str(e)}


def foldctl(*args):
    r = subprocess.run([FOLDCTL, *args], capture_output=True, text=True,
                       timeout=600)
    return {"ok": r.returncode == 0,
            "out": (r.stdout + r.stderr).strip()[-800:]}


class H(BaseHTTPRequestHandler):
    def _human_secret(self):
        m = re.search(r"FOLD_HUMAN_SECRET=(.*)", open(CREDS).read())
        return m.group(1).strip() if m else None

    def _gate(self, method):
        """True if this request may proceed; sends the refusal itself."""
        if self.client_address[0].startswith("127."):
            return True
        # Human plane via the reverse proxy: it authenticates the human
        # and injects this secret; browsers can't forge it past the proxy
        # because the route always overwrites the header.
        hs = self._human_secret()
        if hs and self.headers.get("X-Fold-Human") == hs:
            return True
        tok = board_token()
        presented = (self.headers.get("Authorization") or "")
        if not tok or presented != f"Bearer {tok}":
            self._send(401, {"error": "agent plane requires bearer token"})
            return False
        if (method, self.path) in AGENT_PLANE_PATHS or any(
                m == method and r.match(self.path)
                for m, r in AGENT_PLANE_RES):
            return True
        self._send(403, {"error": "not available on the agent plane "
                         "(dispatch and messaging only)"})
        return False

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        # The UI ships with the server; a cached page after a deploy means
        # missing buttons and confused humans. Never cache.
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._gate("GET"):
            return
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
                    # compare with volatile timestamps masked, so a mere
                    # harvest tick doesn't re-render the client (a jumped
                    # list under a poised cursor is a misclick hazard)
                    cmp = re.sub(r'"t": ?\d+', '"t":0', s)
                    if cmp != last:
                        self.wfile.write(f"data: {s}\n\n".encode())
                        self.wfile.flush()
                        last = cmp
                    time.sleep(3)
            except (BrokenPipeError, ConnectionResetError):
                return
        else:
            m = re.fullmatch(r"/api/task/([\w-]+)/screen", self.path)
            if m:
                return self._proxy_screen(m.group(1))
            m = re.fullmatch(r"/api/task/([\w-]+)/messages", self.path)
            if m:
                ws = m.group(1)
                # the db is the full record (file pull works in any
                # instance state); the agentapi scrape and the archive
                # are fallbacks, in that order
                queued = [q["content"] for q in _outbox().get(ws, [])]
                db = _pull_db(ws, tag="msg")
                msgs = _db_messages(db) if db else None
                if msgs is not None:
                    return self._send(200, {"messages": msgs,
                                            "queued": queued,
                                            "source": "opencode-db"})
                # every fallback carries the outbox too: a hibernated
                # instance's volume is unmountable, so its Visit comes
                # from the archive — which must not hide the mail still
                # waiting for it (observed: queued note vanished from
                # view on reopen, while the outbox held it correctly)
                live = agentapi(ws, "/messages")
                if "error" not in live:
                    live["queued"] = queued
                    return self._send(200, live)
                arch = archived_transcript(ws)
                if arch:
                    arch["queued"] = queued
                    return self._send(200, arch)
                live["queued"] = queued
                return self._send(200, live)
            self._send(404, {"error": "not found"})

    def _proxy_screen(self, ws):
        """Pass agentapi's live-screen SSE through to the browser.

        The browser can't reach the incus bridge; the board can. An idle
        stream is cut by the read timeout — EventSource reconnects and
        agentapi replays the current screen, so the view self-heals.
        """
        ip = instance_ip(ws)
        if not ip:
            return self._send(404, {"error": f"no running instance for {ws}"})
        req = urllib.request.Request(f"http://{ip}:3284/internal/screen",
                                     headers={"Host": "localhost"})
        try:
            up = urllib.request.urlopen(req, timeout=90)
        except Exception as e:
            return self._send(502, {"error": f"screen stream: {e}"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            while True:
                line = up.readline()
                if not line:
                    return
                self.wfile.write(line)
                if line == b"\n":
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return
        finally:
            up.close()

    def _forgejo_hook(self):
        """Review-event doorbell + gate trigger. The forge POSTs review
        events here; the HMAC signature is the whole gate (this path
        never consults the human/agent planes). Changes-requested
        reviews — a human's or bellwether's formal verdict alike — ring
        the author's session through the outbox (reliable delivery),
        with registry-owned routing: head branch -> tasks.json, never
        anything payload-authored. bellwether's non-rejection events
        instead poke scripts/fold-gate, the zero-LLM two-key merge
        executor; the poke carries timing only — the gate re-derives
        every fact from the forge and the registry."""
        n = int(self.headers.get("Content-Length", 0))
        if n > 1_000_000:
            return self._send(413, {"error": "payload too large"})
        raw = self.rfile.read(n)
        sec = webhook_secret()
        sig = (self.headers.get("X-Forgejo-Signature")
               or self.headers.get("X-Gitea-Signature") or "")
        if not sec or not hmac.compare_digest(
                hmac.new(sec.encode(), raw, hashlib.sha256).hexdigest(),
                sig):
            return self._send(401, {"error": "bad signature"})
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        event = (self.headers.get("X-Forgejo-Event")
                 or self.headers.get("X-Gitea-Event") or "")
        rtype = (body.get("review") or {}).get("type") or event
        sender = (body.get("sender") or {}).get("login") or "a reviewer"
        if not event.startswith("pull_request"):
            return self._send(200, {"ok": True, "ignored": event})
        if event == "pull_request":
            # Automatic review invocation (the pipeline's last manual
            # link, closed 2026-11-26): a PR opened or newly pushed on a
            # machine-gated repo gets both lanes. The launcher applies
            # the standing readiness doctrine — the author's CONCLUDED
            # TURN, not the PR API call — waiting for db-verified rest
            # and debouncing the push-per-commit burst, then reviews the
            # head the turn left behind. Policy is registry-owned:
            # review=human repos keep human-invoked reviews.
            action = body.get("action") or ""
            if action not in ("opened", "reopened", "synchronized"):
                return self._send(200, {"ok": True, "ignored": action})
            p = body.get("pull_request") or {}
            rname = (body.get("repository") or {}).get("full_name") or ""
            href = (p.get("head") or {}).get("ref") or ""
            try:
                rreg = json.load(open(os.path.join(STATE_DIR,
                                                   "repos.json")))
            except (FileNotFoundError, json.JSONDecodeError):
                rreg = {}
            proj = next((k for k, v in rreg.items() if isinstance(v, dict)
                         and v.get("repo") == rname
                         and v.get("review") == "bellwether"), "")
            if not proj or not p.get("number"):
                return self._send(200, {"ok": True,
                                        "ignored": "not machine-gated"})
            launcher = os.path.join(HERE, "..", "scripts",
                                    "fold-review-launch")
            with open(os.path.join(STATE_DIR, "review-launch.log"),
                      "ab") as lg:
                subprocess.Popen(
                    [launcher, proj, rname, str(p["number"]), href],
                    stdout=lg, stderr=lg, start_new_session=True)
            print(f"[hook] PR {action} on {rname}#{p.get('number')} -> "
                  "review lanes launching", flush=True)
            return self._send(200, {"ok": True, "review": "launching"})
        pr = body.get("pull_request") or {}
        repo = (body.get("repository") or {}).get("full_name") or ""
        if sender == "bellwether" and "reject" not in rtype \
                and "reject" not in event:
            if repo and pr.get("number"):
                gate = os.path.join(HERE, "..", "scripts", "fold-gate")
                with open(os.path.join(STATE_DIR, "gate.log"), "ab") as lg:
                    subprocess.Popen([gate, repo, str(pr["number"])],
                                     stdout=lg, stderr=lg,
                                     start_new_session=True)
                print(f"[hook] bellwether review on {repo}"
                      f"#{pr.get('number')} -> gate poked", flush=True)
                return self._send(200, {"ok": True, "gate": "poked"})
            return self._send(200, {"ok": True, "ignored": rtype})
        if "reject" not in rtype and "reject" not in event:
            return self._send(200, {"ok": True, "ignored": rtype})
        head = (pr.get("head") or {}).get("ref") or ""
        try:
            reg = json.load(open(os.path.join(STATE_DIR, "tasks.json")))
        except (FileNotFoundError, json.JSONDecodeError):
            reg = {}
        ws = next((k for k, v in reg.items()
                   if isinstance(v, dict) and v.get("branch") == head), "")
        if not ws:
            # follow-up convention <assigned-branch>-...: only the
            # author's own token can have pushed such a branch, so the
            # prefix is as registry-owned as the exact match
            pref = [k for k, v in reg.items() if isinstance(v, dict)
                    and v.get("branch")
                    and head.startswith(v["branch"] + "-")]
            ws = pref[0] if len(pref) == 1 else ""
        if not ws:
            return self._send(200, {"ok": True,
                                    "ignored": "branch not in registry"})
        # Outbox, not a raw send: a mid-turn session swallows typed text
        # without a trace, and a stopped one holds its mail — the
        # delivery thread verifies processing either way.
        enqueue_message(ws, (
            f"{sender} requested changes on your PR "
            f"#{pr.get('number')} ({repo}). The PR is the "
            "conversation: fetch the reviews and comments via "
            "the API, address the points with commits on your "
            "branch, reply on the PR, re-run ./.fold/check, "
            "and comment when ready for re-review."))
        # A parked author cannot receive the ring: wake it, exactly as
        # the gate does for findings — hibernation kept its context
        # warm, and the outbox delivers at its first rest. (With the
        # 24h author park above, a PR awaiting a slow human review can
        # easily outlive its author's wakefulness.)
        st = subprocess.run(
            ["incus", "list", f"fold-{ws.lower()}", "-c", "s",
             "-f", "csv"], capture_output=True, text=True).stdout.strip()
        if st == "STOPPED":
            foldctl("start", ws)
            print(f"[hook] {ws} was parked — started for doorbell "
                  "delivery", flush=True)
        print(f"[hook] review by {sender} on {repo}#{pr.get('number')} -> "
              f"{ws}: queued for verified delivery", flush=True)
        return self._send(200, {"ok": True, "queued": True,
                                "workspace": ws})

    def do_POST(self):
        if self.path == "/api/forgejo-hook":
            return self._forgejo_hook()
        if not self._gate("POST"):
            return
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, {"error": "bad json"})
        if self.path == "/api/dispatch":
            tpl, prompt = body.get("template", ""), body.get("prompt", "")
            if not tpl or not prompt:
                return self._send(400, {"error": "template and prompt required"})
            args = ["dispatch"]
            if body.get("ctx"):
                args += ["--ctx", str(body["ctx"])[:64]]
            # endpoint/model pass through; foldctl validates against the
            # registry, so the agent plane can pick models but not URLs
            if body.get("llm"):
                args += ["--llm", str(body["llm"])[:64]]
            if body.get("model"):
                args += ["--model", str(body["model"])[:128]]
            # project passes through by registry NAME; foldctl validates,
            # so the agent plane picks projects but never clone targets
            if body.get("repo"):
                args += ["--repo", str(body["repo"])[:64]]
            elif body.get("ctx"):
                # registry-owned default: a dispatch from a shepherd
                # session inherits that session's project unless it
                # names one — a shepherd forgetting the field must not
                # quietly land work on the default scratch repo
                try:
                    reg = json.load(open(os.path.join(STATE_DIR,
                                                      "tasks.json")))
                    proj = (reg.get(str(body["ctx"])) or {}).get("project")
                except (FileNotFoundError, json.JSONDecodeError):
                    proj = None
                if proj:
                    args += ["--repo", proj]
            return self._send(200, foldctl(*args, tpl, prompt))
        if self.path == "/api/repos/add":
            # Deliberately NOT in AGENT_PLANE_PATHS: registering repos
            # (and creating them on the forge) is a human decision.
            path = (body.get("path") or "").strip()
            if not re.fullmatch(r"[\w.-]+/[\w.-]+", path):
                return self._send(400, {"error": "path must be owner/name"})
            args = ["repos", "add", path]
            if body.get("create"):
                args.append("--create")
            if body.get("default"):
                args.append("--default")
            if body.get("seed"):
                args.append("--seed")
            if body.get("review") in ("bellwether", "human"):
                args += ["--review", body["review"]]
            name = str(body.get("name") or "")[:32]
            if name and re.fullmatch(r"[\w-]+", name):
                args += ["--name", name]
            return self._send(200, foldctl(*args))
        if self.path == "/api/repos/review":
            # Human plane only (not in the agent allowlists): the review
            # policy IS the merge gate for bellwether-gated repos.
            name = str(body.get("name") or "")[:32]
            mode = body.get("review")
            if not re.fullmatch(r"[\w.-]+", name) or \
                    mode not in ("bellwether", "human"):
                return self._send(400, {"error": "name + review "
                                        "(bellwether|human) required"})
            return self._send(200, foldctl("repos", "review", name, mode))
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
        m = re.fullmatch(r"/api/task/([\w-]+)/keys", self.path)
        if m:
            # Raw keystrokes into the session's terminal: the human
            # typing through the Screen view (harness commands, the odd
            # permission prompt). Human plane only — this path is not in
            # the agent allowlists, so the gate 403s agents above.
            keys = body.get("keys", "")
            if not keys:
                return self._send(400, {"error": "keys required"})
            return self._send(200, agentapi(
                m.group(1), "/message",
                {"content": str(keys)[:256], "type": "raw"}))
        m = re.fullmatch(r"/api/task/([\w-]+)/message", self.path)
        if m:
            content = body.get("content", "").strip()
            if not content:
                return self._send(400, {"error": "content required"})
            ws = m.group(1)
            # A human addressing a parked session wants a conversation,
            # not a mailbox: wake it — hibernation resumes the thread,
            # and the outbox delivers at its first rest after boot.
            # Agent-plane and machinery mail keeps queue-only semantics
            # (the no-wake rule for FYI notices lives there); only the
            # gate's findings bounce wakes, and it does so itself.
            sec = self._human_secret()
            if sec and hmac.compare_digest(
                    self.headers.get("X-Fold-Human", ""), sec):
                st = subprocess.run(
                    ["incus", "list", f"fold-{ws.lower()}", "-c", "s",
                     "-f", "csv"], capture_output=True,
                    text=True).stdout.strip()
                if st == "STOPPED":
                    enqueue_message(ws, content)
                    foldctl("start", ws)
                    print(f"[message] {ws} was parked — started for "
                          "human-plane delivery", flush=True)
                    return self._send(200, {
                        "queued": True, "woke": True,
                        "note": "session was parked — starting it; your "
                                "message delivers when it rests after "
                                "boot"})
            r = agentapi(m.group(1), "/message",
                         {"content": content, "type": "user"})
            if "error" in r:
                # busy or unreachable: the message queues instead of
                # bouncing — the outbox thread delivers at the next
                # stable gap (or next start, for stopped sessions)
                enqueue_message(m.group(1), content)
                return self._send(200, {"queued": True,
                                        "note": r["error"]})
            # Delivery is not processing (third occurrence of this
            # lesson), and the status flip is not delivery either
            # (fourth: keystrokes redraw a TUI into "running" while the
            # typed text vanishes — a notice once passed that check yet
            # never reached the db). Ground truth is the harness db
            # growing the user message; verify that, and tell the
            # sender the truth either way.
            if delivered_to_db(m.group(1), content):
                return self._send(200, r)
            # swallowed: queue a redelivery (worst case a slow db pull
            # makes this a duplicate, which is visible and cheap; a
            # lost message is neither)
            enqueue_message(m.group(1), content)
            r["queued"] = True
            r["note"] = ("typed, but the session's db never recorded it "
                         "— queued for redelivery at the next stable gap")
            return self._send(200, r)
        self._send(404, {"error": "not found"})

    def log_message(self, fmt, *a):
        # mutations deserve a trace (debugging a silent dispatch once
        # meant flying blind); the GET/SSE firehose stays quiet
        if not (a and isinstance(a[0], str) and a[0].startswith("GET")):
            print(f"[{self.client_address[0]}] {fmt % a}", flush=True)


if __name__ == "__main__":
    bind = os.environ.get("FOLD_BOARD_BIND", "127.0.0.1")
    port = int(os.environ.get("FOLD_BOARD_PORT", "7070"))
    threading.Thread(target=archive_transcripts, daemon=True).start()
    threading.Thread(target=deliver_outbox, daemon=True).start()
    print(f"fold board on http://{bind}:{port}")
    ThreadingHTTPServer((bind, port), H).serve_forever()
