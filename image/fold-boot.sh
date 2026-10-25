#!/bin/bash
# fold-boot — brings a fold-native agent to life inside a golden-image
# instance. Everything per-task arrives via incus config keys, readable
# through the guest API at /dev/incus/sock and settable while the
# instance is stopped:
#   user.fold_prompt           the task, verbatim
#   user.fold_env              JSON object of environment variables
#   user.fold_repo             clone URL (tokenless; credentials below)
#   user.fold_role             role contract -> ~/ROLE.md (shepherds)
#   user.fold_opencode_config  opencode.json content
# The home volume persists across restarts; the clone is reused if present.
set -euo pipefail

SOCK=/dev/incus/sock
# user.* config keys are served to root only (socket is world-rw, the
# API discriminates) — same reason the Coder starter fetched via a
# root oneshot. coder has baked NOPASSWD sudo.
key() { sudo curl -sf --unix-socket "$SOCK" "http://localhost/1.0/config/user.fold_$1" || true; }

PROMPT=$(key prompt)
REPO=$(key repo)
CONF=$(key opencode_config)

# Environment: JSON object -> exported vars for the agent process.
ENVJSON=$(key env)
if [ -n "$ENVJSON" ]; then
  while IFS='=' read -r k v; do
    export "$k"="$v"
  done < <(printf '%s' "$ENVJSON" | python3 -c \
    'import json,sys; [print(f"{k}={v}") for k,v in json.load(sys.stdin).items()]')
fi

mkdir -p "$HOME/.config/opencode"
[ -n "$CONF" ] && printf '%s' "$CONF" > "$HOME/.config/opencode/opencode.json"

# Role contract (shepherds): delivered beside the checkout, never inside
# it — the checkout's AGENTS.md addresses the repo's authors, which this
# session may not be. opencode loads it via the config's instructions.
ROLE=$(key role)
[ -n "$ROLE" ] && printf '%s' "$ROLE" > "$HOME/ROLE.md"

# Git credentials: the token never rides in the remote URL — `git
# remote -v` output lands in transcripts (board-archived, agent-plane
# readable) and must stay clean. A credential store feeds git instead;
# the file holds only this agent's own token.
if [ -n "${FORGEJO_TOKEN:-}" ] && [ -n "${FORGEJO_URL:-}" ]; then
  proto=${FORGEJO_URL%%://*}; host=${FORGEJO_URL#*://}
  printf '%s://%s:%s@%s\n' "$proto" "${GIT_AUTHOR_NAME:-fold}" \
    "$FORGEJO_TOKEN" "${host%%/*}" > "$HOME/.git-credentials"
  chmod 600 "$HOME/.git-credentials"
  git config --global credential.helper store
fi

WORKDIR="$HOME"
if [ -n "$REPO" ]; then
  NAME=$(basename "$REPO" .git)
  WORKDIR="$HOME/$NAME"
  [ -d "$WORKDIR/.git" ] || git clone -q "$REPO" "$WORKDIR"
fi
cd "$WORKDIR"

ARGS=(server --type opencode --term-width 120 --term-height 40)
[ -n "$PROMPT" ] && ARGS+=(-I "$PROMPT")
exec agentapi "${ARGS[@]}" -- opencode
