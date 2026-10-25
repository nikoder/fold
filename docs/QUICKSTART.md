# Quickstart (honest edition)

fold is pre-alpha and extracted from one homelab. This is the shape of
a deployment, not yet a turnkey path. Everything specific to your
deployment lives in two small uncommitted files (steps 1–2 below); see
Portability in the README for what is still assumed.

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
   board's agent-plane bearer token, the human-plane header secret, a
   webhook secret (`FOLD_WEBHOOK_SECRET`, for the review-feedback
   doorbell `foldctl repos add` wires up), and your inference keys. Default path `~/.config/fold/creds.env`; set
   `FOLD_CREDS` to move it.
   **Never put this file inside a git repository.** If it sits inside
   a checkout of a repo agents work on, every agent clone would carry
   your keys — foldctl and the board REFUSE to run in that case
   (re-checked on every dispatch). Inside any other git repo, a
   checkout with live tokens is still one `git add -A` away from
   publishing them, so they warn loudly and leave the call to you.
2. **Deployment file**: `$FOLD_STATE_DIR/fold.env` (default
   `~/.local/state/fold/`), plain KEY=VALUE, process env overrides —
   `FOLD_FORGEJO` (base URL), `FOLD_WORK_REPO` (owner/repo agents work
   on), `FOLD_NTFY` (full topic URL), `FOLD_BOARD_URL` (bridge-reachable,
   for shepherds), optional `FOLD_SHEPHERD_REPO` and
   `FOLD_AUTHOR_LOGIN`. Nothing in the repo defaults to any particular
   deployment; a missing value fails loudly at the verb that needs it.
3. **Accounts.** Forgejo users for each author agent (e.g.
   `fold-alpha`) and `bellwether`; collaborator rights on target repos;
   branch protection: 1 required approval, no direct pushes to the
   default branch. ntfy users: agent identity write-only on `fold`,
   board identity read-only.
4. **Egress ACL** (observe first, restrict later):
   `incus network acl create fold-egress`, one egress `allow`+logged
   rule, one ingress `allow`. `scripts/egress-report.sh` turns the logs
   into your eventual allowlist. **Phase two**, once the report shows
   only expected flows: replace the catch-all with explicit allows —
   typically tcp/443 to your reverse proxy (it fronts forge, inference,
   and ntfy alike), tcp/7070 + udp+tcp/53 to the incus bridge IP — and
   set `security.acls.default.egress.action=reject` plus
   `...default.egress.logged=true` on the profile's eth0, so the wall
   keeps reporting what it stops. Gotcha: in-place ACL rule edits can
   fail with an nft "no such chain" error (chain naming drift); create
   a fresh ACL with the new rules and point the profile at it instead.
   **Dependencies behind the wall**: don't punch registry holes in the
   ACL (npm rides CDN anycast — allowlisting its IPs allowlists half
   the internet). Run a read-through cache behind your reverse proxy
   instead (verdaccio for npm; it must rewrite tarball URLs, a plain
   proxy route can't) and hand agents the route via `FOLD_TASK_ENV`,
   e.g. `{"NPM_CONFIG_REGISTRY":"https://npm.example.com"}`. No
   services of your own? Point the same variable at the public
   registry and add a matching egress allow — your wall, your
   trade-off.
5. **Golden image**: pin a base locally
   (`incus image copy images:debian/13/cloud local: --alias
   fold-builder-base` — never launch from `images:` at runtime, the
   remote update check can stall forever), then
   `./scripts/build-image.sh`. Read that script's comments: every one
   is a scar.
6. **Endpoint registry**: `$FOLD_STATE_DIR/endpoints.json` (default
   `~/.local/state/fold/`) naming your inference endpoints, models, and
   the default pair.
7. **Board**: `docker compose up -d --build` from the repo root. Put
   an auth gate on a reverse-proxy route (anything from basic auth to a
   WebAuthn forward_auth service) that injects the `X-Fold-Human`
   header secret on success; loopback and the agent bearer plane work
   as-is.
8. **Contract**: copy `contract/AGENTS.template.md` into each repo
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
