#!/usr/bin/env bash
# egress-report — summarize observed fold egress (phase one: logged, not
# locked). Reads the kernel journal lines the fold-egress ACL emits and
# prints destination:port flow counts per source instance, so the phase-two
# allowlist is written from observation, not guesswork.
#
# Usage: egress-report.sh [journalctl-since, default "-7d"]
set -euo pipefail
SINCE=${1:--7d}
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
journalctl -k --since "$SINCE" --no-pager 2>/dev/null \
  | grep -F "eth0-egress" > "$TMP" || true
python3 - "$TMP" <<'EOF'
import collections
import re
import socket
import sys

flows = collections.Counter()
for line in open(sys.argv[1]):
    m = re.search(r"SRC=(\S+) DST=(\S+) .*?PROTO=(\S+)(?: SPT=\S+ DPT=(\S+))?", line)
    if not m:
        continue
    src, dst, proto, dport = m.groups()
    flows[(src, dst, proto, dport or "-")] += 1

names = {}
for (_, dst, _, _) in flows:
    if dst not in names:
        try:
            names[dst] = socket.gethostbyaddr(dst)[0]
        except OSError:
            names[dst] = "-"

print(f"{'count':>7}  {'source':<16} {'dest':<40} {'proto':<5} port")
for (src, dst, proto, dport), n in flows.most_common(60):
    label = dst if names[dst] == "-" else f"{dst} ({names[dst]})"
    print(f"{n:>7}  {src:<16} {label:<40} {proto:<5} {dport}")
EOF
