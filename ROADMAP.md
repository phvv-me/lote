# Roadmap

Where lote is headed. lote is one bridge over many machines. It ships your repo
to a host, runs the job under whatever scheduler that host has, and pulls the
results back. This document tracks what works today and what each milestone
needs. It is a direction, not a contract, so order and scope can shift.

## Today (0.0.x)

The core loop works end to end.

- [x] One bridge over four backends. `pueue` on a DGX or PC over ssh, `sbatch` on
  a SLURM cluster, `qsub` on PBS, and plain `bash` on a host with no scheduler.
- [x] Three layers. The control plane `lote`, the on-host executor `lote exec`,
  and typed clients that wrap `qsub`, `sbatch`, `pueue`, and `rsync`.
- [x] Onboarding that earns a place in the lote. `lote setup` probes the host,
  rsyncs the repo, and runs `chefe install`, so only machines that build the env
  are cached as targets.
- [x] Dispatch and routing. `lote submit <target> <script>`, plus `submit auto`
  that picks the smallest host that fits a `--needs <GB>` request.
- [x] Tracking and pull-back. `ps`, `status`, `reconcile`, `logs`, `info`,
  `fetch`, `pull`, `watch`, and a recorded command `history`, all backed by one
  TinyDB `.lote/db.json`.
- [x] Targets from `~/.ssh/config`, config from a tiny `lote.toml`, and a job
  script that runs anywhere with no code changes.

## v0.1.0

Make lote complete and trustworthy across every host it touches.

- [ ] **More scheduler backends.** Add LSF (`bsub`) and Kubernetes (`Job`/`kubectl`)
  alongside pueue, SLURM, and PBS, behind the same `submit`/`status`/`logs`
  contract so a job script still runs anywhere.
- [ ] **Richer `reconcile` and auto-retry.** Detect a vanished or failed run and
  offer to resubmit it, with a policy for how many times and how far back.
- [x] **A `cyclopts` CLI.** Moved off `fire` to `cyclopts` for typed arguments,
  real help, and shell completions, without changing the command surface.
- [ ] **Friendlier failures.** Clear messages when a host is unreachable, an env
  fails to build, or a scheduler is missing, plus a `lote doctor` that checks a
  target and reports what is off.
- [ ] **Richer `lote ls`.** Show drift between cached capabilities and the live
  host, and what an onboard would change.
- [ ] **LLM-friendly docs.** Polish the generated `llms.txt` and `llms-full.txt`
  so assistants get clean, sectioned context, keep them building in CI, and link
  them from the README.
- [ ] **Docs i18n.** Hand-translate the docs to pt-BR, es, ja, and zh, and keep
  every page in sync.
- [ ] **PyPI trusted publishing.** Cut releases from CI with OIDC, no long-lived
  token.

## v1.0.0

Freeze the surface and make lote safe to depend on.

- [ ] **Stable config and command surface.** A versioned `lote.toml` schema for
  editor autocomplete and validation, with a written migration and deprecation
  policy.
- [ ] **A scheduler plugin API** so a new backend can be added without touching
  core. The hard-coded schedulers become a registry others can extend.
- [ ] **Full tested parity** across pueue, SLURM, PBS, LSF, and Kubernetes, on
  every host kind lote advertises.
- [ ] **A web dashboard.** A read-only view of runs across the whole lote, live
  state and logs, on top of the same `.lote/db.json` and `reconcile` data.
- [ ] **Robust self-management.** A `lote self update`, a hardened installer,
  and signed releases.
- [ ] **Compatibility guarantees.** Semantic versioning, a deprecation policy,
  and a 1.x promise for the config and CLI.
- [ ] **Complete reference docs** in every supported language.

## Later

Ideas worth doing once the milestones above land.

- [ ] Multi-repo and monorepo dispatch from one bridge.
- [ ] Cost and queue awareness in `submit auto`, routing by price and wait time,
  not just by VRAM.
- [ ] Shared run history across collaborators, so a team sees one lote.
- [ ] An editor extension on top of the published `lote.toml` schema.
