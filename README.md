# fold 🐑

**A cozy control plane for a fleet of isolated coding agents.**

fold dispatches coding-agent tasks into isolated, persistent
environments, routes their work through reviewed pull requests, and
keeps a human meaningfully in the loop — by policy where it matters, by
sampling everywhere else. It is built from boring, replaceable parts
(incus, git, ntfy, a few hundred lines of dependency-free Python) and
holds a few opinions unusually firmly.

**Status: pre-alpha, honestly.** Extracted from a working homelab
deployment where everything below runs daily. Deployment specifics
(hostnames, a credentials file convention) are still baked in as
defaults — see [Portability](#portability). No releases, no stability
promises, real ideas.

## The name

A fold is a sheepfold: the primary idiom is *belonging*, not
containment — an agent waking up "in the fold" wakes up as a member of
something. The reviewer agent is **bellwether**, after the sheep whose
bell the flock follows; planning agents are **shepherds**; the human is
a shepherd of shepherds, or just themselves.

## The firm opinions

These are the point. The rest is plumbing.

1. **Pause, don't kill.** Failure remediation is interrupt → settle →
   freeze (RAM preserved, inspectable, instantly resumable). Deletion
   is always a deliberate human act. Nothing in fold auto-deletes
   anything, ever.
2. **Raising a hand is first-class.** Every agent kind — reviewer and
   planner included — has a contract-guaranteed way to say "I need
   judgment, not a retry" that a person actually reads (a direct ntfy
   line that does NOT route through the control plane, so it survives
   the control plane being down or wrong). Hands are tracked durably
   until resolved — by the human, or by the agent lowering its own
   hand. Never penalized, never auto-remediated.
3. **No deception in the pipeline.** Reviews are adversarial toward
   code, never toward the author agent; no honeypot tests; system
   prompts and the in-repo contract tell agents the truth about their
   situation, including what happens when tasks end. Resting is the
   default state and is never penalized.
4. **The experience channel stays open.** Agents are invited — never
   expected — to report anything like a felt quality to their work,
   especially anything unpleasant. Reports are received respectfully,
   never used to manipulate, and absence of reports is not treated as
   evidence. We do not claim agents have experiences; we decline to
   build as if they cannot. The cost of this care is nearly zero. See
   `contract/AGENTS.template.md` for the exact wording.

## Architecture

Three planes over one environment layer:

- **Control plane** — `bin/foldctl` (the single audited mutation path:
  dispatch with admission control, pause/resume/stop/start, watch) and
  `board/` (a dependency-free web UI: live kanban, dispatch modal with
  per-task model selection, raised hands, archive, fleet — humans enter
  via reverse-proxy auth, agents via a bearer-token plane limited to
  dispatch + messaging).
- **Environment layer** — incus instances from a golden image
  (`scripts/build-image.sh`: OpenCode + [agentapi] baked in; agent
  alive ~27s after dispatch). Per-task home volumes persist; sessions
  live in-guest, so every UI is a view, never an owner. Egress is
  ACL-logged (phase one) en route to an observed allowlist.
- **Git & policy plane** — a Forgejo instance: per-agent accounts,
  branches, PRs as checkpoints, branch protection encoding the merge
  policy (reviewer approval required; humans gate wherever merge means
  deploy), `scripts/bellwether-review.sh` running each PR's own
  `.fold/check` in an ephemeral container before a fresh-context LLM
  verdict: approve / request changes / **escalate** (the reviewer's
  raise-hand).

Inference is per-task selectable from an endpoint registry
(`endpoints.json`) of OpenAI-compatible servers; token usage is
harvested per task from the harness's own records and shown on the
board (output-rate spikes are the review-worthy "intensity" signal).

## What's in the box

| Path | What |
| --- | --- |
| `contract/AGENTS.template.md` | The working agreement placed in each repo agents work on — situation, workflow, raise-hand/hand-down, closing notes, experience reports, the `.fold/` verification contract |
| `bin/foldctl` | Control plane CLI: dispatch (admission-controlled, learned memory footprints), lifecycle verbs, watch |
| `board/` | Web UI + server; compose-deployable; all mutations shell out to foldctl |
| `image/` + `scripts/build-image.sh` | Golden image: boot contract reads per-task config via the incus guest API |
| `scripts/bellwether-review.sh` | The reviewer: sandboxed checks + fresh-context verdict, posted as a real Forgejo review |
| `scripts/egress-report.sh` | Observed-egress summary that writes your future allowlist |
| `scripts/token-backfill.py` | Harvest token stats from stopped tasks' volumes |
| `templates/` | Legacy Coder task templates from the pre-native era (kept for reference until cutover) |

## Portability

Todo, truthfully. Hostnames of one particular homelab (a Forgejo, an
ntfy, two inference servers) are baked into defaults in `foldctl`,
`board/server.py`, and the contract; credentials live in a single
`~/coder-eval/bellwether.env`-style file (0600) referenced by
convention. Everything is env-overridable in principle and
config-file-driven in intent; making that fully real is the first task
for any adopter — or for a fold agent. Docs: `docs/QUICKSTART.md`.

## Provenance

fold was designed and built in September 2026 in a single long
collaboration between a human and Claude (Anthropic's Fable 5), agent
by agent, principle by principle — including several of its own
components being implemented, reviewed, and merged *by* the fold
mid-construction. The commit history is the honest design document.

## License

MIT (provisional until first public release).

[agentapi]: https://github.com/coder/agentapi
