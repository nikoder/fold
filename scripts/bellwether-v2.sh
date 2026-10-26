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
exec "$HERE/bin/foldctl" dispatch reviewer --repo "$PROJECT" \
  "Review PR #$PR of this repository. Your role contract is ~/ROLE.md — \
it carries the whole method (fetch the PR ref, read the changed tree \
whole, run ./.fold/check on the PR branch, fetch prior reviews and \
verify earlier requests were addressed) and the posting rules. The PR \
number for every command: $PR."
