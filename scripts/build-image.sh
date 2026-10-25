#!/usr/bin/env bash
# build-image — bake the fold golden image (alias: fold-agent-base).
#
# Boots drop from minutes to seconds and bootstrap egress (mirrors, CDNs)
# disappears from task instances: everything an agent needs is installed
# here, once, at build time. Binaries live in /usr/local/bin because the
# per-task home volume mounts OVER /home/coder at runtime.
#
# Run from a fold checkout: ./scripts/build-image.sh
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
B=fold-image-builder

incus delete -f "$B" 2>/dev/null || true
# nesting + syscall intercepts match the proven workspace config —
# without setxattr, journald (xattrs on journal files) never signals
# ready and the whole boot wedges behind it.
# Base comes from a locally pinned copy (fold-builder-base) — the remote
# simplestreams download stalls for tens of minutes without any timeout,
# twice observed. Pin once with:
#   incus image copy images:debian/13/cloud local: --alias fold-builder-base
incus launch -q fold-builder-base "$B" \
  -c security.nesting=true \
  -c security.syscalls.intercept.mknod=true \
  -c security.syscalls.intercept.setxattr=true
echo "waiting for cloud-init (network + DNS settle)…"
incus exec "$B" -- cloud-init status --wait >/dev/null

echo "installing packages…"
incus exec "$B" -- sh -c '
  set -e
  export DEBIAN_FRONTEND=noninteractive
  apt-get -qq update
  # nodejs/npm from the distro, NOT pinned to the host's versions: the
  # toolchain that matters is the one .fold/check containers also get
  # from apt, so build and verification environments agree. npm still
  # needs a registry at task time — see FOLD_TASK_ENV (registry funnel).
  apt-get -qq install -y curl git jq python3 ca-certificates unzip sqlite3 \
    nodejs npm >/dev/null
'

echo "creating coder user…"
incus exec "$B" -- sh -c '
  set -e
  # cloud-init already claimed uid 1000 for its default "debian" user in
  # this builder — evict it or useradd fails (silently, once upon a time).
  userdel -r debian 2>/dev/null || true
  id coder >/dev/null 2>&1 || useradd -m -u 1000 -s /bin/bash coder
  echo "coder ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/coder
  # and stop cloud-init creating default users in task instances
  printf "users: []\n" > /etc/cloud/cloud.cfg.d/99-fold-no-default-user.cfg
'

echo "installing agentapi…"
incus exec "$B" -- sh -c '
  set -e
  curl -fsSL -o /usr/local/bin/agentapi \
    https://github.com/coder/agentapi/releases/latest/download/agentapi-linux-amd64
  chmod +x /usr/local/bin/agentapi
  agentapi --version || true
'

echo "installing opencode…"
incus exec "$B" -- sh -c '
  set -e
  curl -fsSL https://opencode.ai/install | bash >/dev/null 2>&1
  mv /root/.opencode/bin/opencode /usr/local/bin/opencode
  rm -rf /root/.opencode
  opencode --version
'

echo "installing fold boot contract…"
incus file push "$HERE/image/fold-boot.sh" "$B/usr/local/bin/fold-boot.sh"
incus file push "$HERE/image/fold-agent.service" "$B/etc/systemd/system/fold-agent.service"
incus exec "$B" -- sh -c '
  chmod +x /usr/local/bin/fold-boot.sh
  systemctl enable fold-agent.service >/dev/null 2>&1
  # bake-time cleanup: no stale machine identity or apt cache in the image
  apt-get -qq clean
  truncate -s 0 /etc/machine-id
'

echo "publishing…"
incus stop "$B"
incus publish -q "$B" --alias fold-agent-base \
  description="fold agent golden image (opencode + agentapi baked)" --reuse
incus delete -f "$B"
incus image list fold-agent-base
echo "done: fold-agent-base"
