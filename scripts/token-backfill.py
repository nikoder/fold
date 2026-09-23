#!/usr/bin/env python3
"""token-backfill — harvest token usage from STOPPED tasks' home volumes.

The live harvester only sees running instances; older tasks predate it
entirely. This attaches each stopped task's home volume to a scratch
container, reads opencode's sqlite (WAL replayed), and fills the gaps
in tokens.json. Existing entries are never overwritten.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.request

STATE = os.path.expanduser(os.environ.get("FOLD_STATE_DIR",
                                          "~/.local/state/fold"))
TOKENS = os.path.join(STATE, "tokens.json")
CODER = os.environ.get("FOLD_CODER_URL", "http://localhost:7081")
TOKEN_FILE = os.path.expanduser(
    os.environ.get("FOLD_CODER_TOKEN_FILE", "~/coder-eval/.api-token-tasks"))
SCRATCH = "fold-backfill"


def incus(*args, ok_fail=False):
    r = subprocess.run(["incus", *args], capture_output=True, text=True)
    if r.returncode and not ok_fail:
        sys.exit(f"incus {' '.join(args)}: {r.stderr.strip()}")
    return r


def volumes():
    r = incus("storage", "volume", "list", "default", "--format", "csv",
              "-c", "n")
    return {l.strip() for l in r.stdout.splitlines() if l.strip()}


def coder_workspaces():
    try:
        tok = open(TOKEN_FILE).read().strip()
        req = urllib.request.Request(
            f"{CODER}/api/experimental/tasks?owner=me",
            headers={"Coder-Session-Token": tok})
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.load(r)
        tasks = d if isinstance(d, list) else d.get("tasks", [])
        return {t["workspace_name"]: t["workspace_id"] for t in tasks}
    except Exception as e:
        print(f"(coder unavailable, native volumes only: {e})")
        return {}


def harvest(volume):
    """Attach volume to the scratch container, sum opencode tokens."""
    incus("storage", "volume", "attach", "default", volume, SCRATCH, "/mnt")
    try:
        with tempfile.TemporaryDirectory() as td:
            base = f"{SCRATCH}/mnt/.local/share/opencode/opencode.db"
            for suffix in ("", "-wal", "-shm"):
                incus("file", "pull", base + suffix,
                      os.path.join(td, "db" + suffix), ok_fail=True)
            db = os.path.join(td, "db")
            if not os.path.exists(db):
                return None
            con = sqlite3.connect(db)  # writable copy: WAL must replay
            tin = tout = msgs = 0
            for (data,) in con.execute("SELECT data FROM message"):
                try:
                    d = json.loads(data)
                except json.JSONDecodeError:
                    continue
                tk = d.get("tokens") or {}
                if d.get("role") == "assistant" and tk:
                    tin += tk.get("input", 0)
                    tout += tk.get("output", 0)
                    msgs += 1
            con.close()
            return {"input": tin, "output": tout, "msgs": msgs}
    except sqlite3.Error:
        return None
    finally:
        incus("storage", "volume", "detach", "default", volume, SCRATCH,
              ok_fail=True)


def main():
    try:
        tokens = json.load(open(TOKENS))
    except (FileNotFoundError, json.JSONDecodeError):
        tokens = {}
    vols = volumes()
    running = {l.split(",")[0] for l in incus(
        "list", "--format", "csv", "-c", "ns").stdout.splitlines()
        if l.endswith("RUNNING")}

    candidates = {}  # workspace -> volume
    for v in vols:
        m = re.fullmatch(r"fold-(task-\d+)-home", v)
        if m:
            candidates[m.group(1)] = v
    for ws, wid in coder_workspaces().items():
        if f"coder-{wid}-home" in vols:
            candidates.setdefault(ws, f"coder-{wid}-home")

    todo = {ws: v for ws, v in candidates.items()
            if ws not in tokens and f"fold-{ws}" not in running}
    if not todo:
        print("nothing to backfill")
        return
    print(f"backfilling {len(todo)}: {sorted(todo)}")
    incus("delete", "-f", SCRATCH, ok_fail=True)
    # LOCAL image only: images: aliases phone home for an update check
    # that can stall indefinitely (thrice-learned lesson). The first run
    # of this script hung 10 minutes on exactly that.
    incus("launch", "-q", "fold-agent-base", SCRATCH)
    try:
        for ws, vol in sorted(todo.items()):
            rec = harvest(vol)
            if rec:
                rec.update(t=0, out_per_min=0)
                tokens[ws] = rec
                print(f"  {ws}: in={rec['input']} out={rec['output']} "
                      f"msgs={rec['msgs']}")
            else:
                print(f"  {ws}: no opencode data")
    finally:
        incus("delete", "-f", SCRATCH, ok_fail=True)
    json.dump(tokens, open(TOKENS, "w"))
    print("tokens.json updated")


if __name__ == "__main__":
    main()
