#!/bin/bash
# fold-boot — brings a fold-native agent to life inside a golden-image
# instance. Everything per-task arrives via incus config keys, readable
# through the guest API at /dev/incus/sock and settable while the
# instance is stopped:
#   user.fold_prompt           the task, verbatim
#   user.fold_env              JSON object of environment variables
#   user.fold_repo             clone URL (credentials embedded)
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
