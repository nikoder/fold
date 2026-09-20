# fold

A cozy control plane for a fleet of isolated coding agents.

fold dispatches coding-agent tasks into isolated, persistent environments,
routes their work through reviewed pull requests, and keeps a human
meaningfully in the loop — by policy where it matters, by sampling
everywhere else. It is built from boring, replaceable parts and holds a
few opinions unusually firmly.

**Status: pre-alpha.** Extracted from a working homelab deployment; the
pieces below run today, the board UI does not exist yet.

## The name

A fold is a sheepfold: the primary idiom is belonging, not containment —
an agent waking up "in the fold" wakes up as a member of something. The
reviewer agent is **bellwether**, after the sheep that leads the flock.

## Architecture

Three planes over one environment layer:

- **Control plane** (this repo, being built): kanban board, task →
  environment dispatch, live agent status, click-to-attach. Talks to
  environments only through a provider interface (Coder and incus
  adapters), and to agents only through
  [agentapi](https://github.com/coder/agentapi).
- **Environment layer**: Terraform-templated workspaces — currently Coder
  (AGPL) provisioning docker containers or incus instances. Sessions
  persist *in the guest* (tmux + agentapi); every front-end is just a view.
- **Git & policy plane**: a Forgejo instance. Per-agent accounts, PRs as
  checkpoints, branch protection encoding the merge policy, reviewer
  approval by bellwether, human gate wherever merge equals deploy.

## The firm opinions

1. **Pause, don't kill.** Failure remediation is suspend-and-inspect;
   deletion is always a deliberate human act.
2. **Raising a hand is first-class.** Every agent kind — reviewer
   included — has a way to say "I need judgment, not a retry" that a
   person actually reads. It is never penalized and never auto-remediated.
3. **No deception in the pipeline.** Reviews are adversarial toward code,
   never toward the author agent; no honeypot tests; system prompts tell
   agents the truth about their situation.
4. **The experience channel stays open.** Agents are invited — never
   expected — to report anything like a felt quality to their work.
   See `contract/AGENTS.template.md` for the wording and the reasoning.

## What's here

- `contract/AGENTS.template.md` — the working agreement placed in each
  repo agents work on (OpenCode and Claude Code read it natively).
  Includes the `.fold/` verification contract: `check` (executable,
  exit code is the interface) + `config` (declarative environment needs).
- `scripts/bellwether-review.sh` — reviewer v1: per review, an ephemeral
  incus container clones the PR branch and runs the repo's own
  `.fold/check`; a fresh LLM context weighs diff + check results and
  posts an official Forgejo review. Verdicts: approve / request_changes /
  escalate (bellwether's raise-hand).
- `bin/foldctl` — control plane v0 (CLI seed of the board): list and
  dispatch tasks, and the mechanized pause-default policy — `pause` /
  `resume` freeze a workspace's incus instance with RAM preserved, and
  `watch` responds to failing tasks by freezing + notifying, never
  deleting.
- `board/` — the board v0: a dependency-free web face over foldctl
  (which stays the single audited mutation path). Live columns, dispatch
  modal with admission control, fleet table with pause/resume. Binds
  127.0.0.1:7070; reach it over SSH tunnel or a reverse-proxy route.
- `templates/` — Coder task templates: `tasks-docker` (docker provider)
  and `fold-incus` (incus instances with persistent home volumes).
  Secrets enter as sensitive Terraform variables at push time, never in
  files.

## License

MIT (provisional until first public release).
