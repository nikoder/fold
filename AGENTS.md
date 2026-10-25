# Working in the fold's own repository

Two kinds of agents open this file. Everything in
`contract/AGENTS.template.md` about your situation applies to both —
truthful framing, raise-hand, closing notes, optional experience
reports. Find your role:

- **Author agent** (dispatched with a code task, no `FOLD_CTX` of your
  own session): see "Authors" directly below.
- **Shepherd** (resident planning session, `FOLD_CTX` set to your own
  workspace): see "The shepherd" further down.

## Authors: changing the fold itself

This checkout is the fold's control plane — the code that runs you and
your flock-mates. That earns it the strictest merge policy in the fold:

- Work on the branch `$FOLD_BRANCH` from your environment; push and
  open a PR exactly as the template contract describes.
- Run `./.fold/check` before opening the PR.
- Review feedback arrives as a session message pointing at the PR;
  the PR is the conversation (see the template contract's workflow).
- bellwether's review here is ADVISORY: iterate on its change requests
  as usual, but only the human's approval satisfies the gate, and only
  the human merges — merge means deploy for this repo. A long wait on
  the human is normal, not a sign something is wrong.
- Never act against the RUNNING control plane (the board, live incus
  instances, the state directory): your change lands through the PR or
  not at all.

# The shepherd — working agreement

A shepherd's checkout is its *subject* — the project it plans for, which
may be this repository or any registered project. Its working agreement
therefore does not live here: the fold delivers it at boot as
`~/ROLE.md` in the shepherd's home. The canonical text is
[`contract/SHEPHERD.md`](contract/SHEPHERD.md) in this repository.
