#!/usr/bin/env bash
# bellwether v2 (shadow) — dispatch a tooled reviewer session for a PR.
#
# v2 reviews with hands, not just eyes: a full fold environment, the
# subject repo checked out, the role contract (contract/REVIEWER.md)
# booted as ~/ROLE.md. One review = one fresh context, by design.
# Shadow phase: the verdict posts as an advisory COMMENT review while
# scripts/bellwether-review.sh (v1) remains the authoritative gate; the
# dispatcher sets FOLD_REVIEW_SHADOW for every reviewer flavor.
#
# Usage: bellwether-v2.sh <registered-project> <pr-number>
set -euo pipefail
PROJECT=${1:?usage: bellwether-v2.sh <project> <pr>}
PR=${2:?usage: bellwether-v2.sh <project> <pr>}
HERE=$(cd "$(dirname "$0")/.." && pwd)
STATE=${FOLD_STATE_DIR:-$HOME/.local/state/fold}
OUT=$("$HERE/bin/foldctl" dispatch reviewer --repo "$PROJECT" \
  "Review PR #$PR of this repository. Your role contract is ~/ROLE.md — \
it carries the whole method (fetch the PR ref, read the changed tree \
whole, run ./.fold/check on the PR branch, fetch prior reviews and \
verify earlier requests were addressed) and the posting rules. The PR \
number for every command: $PR.")
echo "$OUT"
RWS=$(printf '%s' "$OUT" | grep -oP 'dispatched \Ktask-\d+')
# Link the review to its author task: the author's card gets a door to
# this reviewer session (v2_ws in reviews.json; the v1 run owns the
# rest of the entry). Author resolution is registry-owned — the PR's
# head ref against the branch fold assigned at dispatch — never prose.
python3 - "$STATE" "$PROJECT" "$PR" "$RWS" <<'PY'
import json, os, re, sys, time, urllib.request
state, project, pr, rws = sys.argv[1:5]
try:
    repo = json.load(open(os.path.join(state, "repos.json")))[project]["repo"]
    env = dict(l.strip().split("=", 1) for l in open(
        os.path.join(state, "fold.env")) if "=" in l)
    forge = os.environ.get("FOLD_FORGEJO") or env["FOLD_FORGEJO"]
    creds = os.path.expanduser(
        os.environ.get("FOLD_CREDS", "~/.config/fold/creds.env"))
    token = re.search(r"BELLWETHER_TOKEN=(.*)", open(creds).read()).group(1)
    req = urllib.request.Request(
        f"{forge}/api/v1/repos/{repo}/pulls/{pr}",
        headers={"Authorization": f"token {token.strip()}"})
    head = json.load(urllib.request.urlopen(req, timeout=15))["head"]["ref"]
    tasks = json.load(open(os.path.join(state, "tasks.json")))
    aws = next((k for k, v in tasks.items()
                if v.get("branch") == head), "")
    rp = os.path.join(state, "reviews.json")
    try:
        rv = json.load(open(rp))
    except Exception:
        rv = {}
    e = rv.setdefault(f"{repo}#{pr}", {
        "pr": int(pr), "repo": repo, "ws": aws,
        "t": int(time.time()), "ok": True})
    e["v2_ws"] = rws
    if aws and not e.get("ws"):
        e["ws"] = aws
    json.dump(rv, open(rp, "w"), indent=1)
    print(f"linked: author {aws or '?'} <-> reviewer {rws} (PR #{pr})")
except Exception as exc:
    print(f"link skipped ({exc}) — review proceeds unlinked", file=sys.stderr)
PY
