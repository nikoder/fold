# Quickstart (honest edition)

fold is pre-alpha and extracted from one homelab. This is the shape of
a deployment, not yet a turnkey path — expect to adapt hostnames and
the credentials convention (see Portability in the README).

## You need

- **incus** (6.x) with a `default` storage pool and `incusbr0` network
- **Forgejo** (or Gitea) — repos, per-agent accounts, branch protection
- **ntfy** (deny-all auth) — the raise-hand channel, plus your phone
- An **OpenAI-compatible inference endpoint** (vLLM/llama-swap,
  Lemonade, …)
- **docker compose** for the board; a reverse proxy (Caddy) for the
  human front door

## Pieces, in order

1. **Credentials file** (0600). One env-style file holding: a Forgejo
   token for the author agent, one for the reviewer, an ntfy write-only
   token for topic `fold` (+ a read-only one for the board), the
   board's agent-plane bearer token, the human-plane header secret, and
   your inference keys. Defaults point at
   `~/coder-eval/bellwether.env`; set `FOLD_CREDS` to move it.
2. **Accounts.** Forgejo users for each author agent (e.g.
   `fold-alpha`) and `bellwether`; collaborator rights on target repos;
   branch protection: 1 required approval, no direct pushes to the
   default branch. ntfy users: agent identity write-only on `fold`,
   board identity read-only.
3. **Egress ACL** (observe first, restrict later):
   `incus network acl create fold-egress`, one egress `allow`+logged
   rule, one ingress `allow`. `scripts/egress-report.sh` turns the logs
   into your eventual allowlist.
4. **Golden image**: pin a base locally
   (`incus image copy images:debian/13/cloud local: --alias
   fold-builder-base` — never launch from `images:` at runtime, the
   remote update check can stall forever), then
   `./scripts/build-image.sh`. Read that script's comments: every one
   is a scar.
5. **Endpoint registry**: `$FOLD_STATE_DIR/endpoints.json` (default
   `~/.local/state/fold/`) naming your inference endpoints, models, and
   the default pair.
6. **Board**: `docker compose up -d --build` from the repo root. Put
   basic auth + an injected `X-Fold-Human` header secret on a reverse
   proxy route; loopback and the agent bearer plane work as-is.
7. **Contract**: copy `contract/AGENTS.template.md` into each repo
   agents will work on (as `AGENTS.md`), add a `.fold/check`
   (executable, exit 0 = pass) and `.fold/config` (image/packages/
   timeout for the check container).

## First dispatch

    bin/foldctl dispatch agent 'Your checkout is the current directory
    (<repo>). Follow AGENTS.md. Task: <only the work itself>.'

Watch it on the board. Review its PR with
`scripts/bellwether-review.sh <pr-number>`. Merge is yours (or branch
protection's).

## The parts you must not skip

The contract, the raise-hand plumbing, and the pause-don't-kill
lifecycle are not optional garnish on a task runner — they are the
project. If you deploy the dispatch loop without the working agreement,
you have built a different, worse thing and should call it something
else.
