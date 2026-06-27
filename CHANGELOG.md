# Changelog

All notable changes to lote are documented here.

The format follows Keep a Changelog, and releases are cut from the version in `pyproject.toml`.

## Unreleased

### Added

- A `--mem <GB>` option on `submit` and `run`, so a memory-hungry job requests the headroom it needs instead of being OOM-killed. It maps per backend: PBS joins `mem=NNgb` to the `select=` chunk, SLURM passes `--mem=NNG`. The 14B/32B cluster jobs that were SIGKILLed (exit 137) under the default grant can now ask for the memory they need up front.

### Changed

- A generated `--cmd` job now hands its `--gpus`/`--walltime`/`--mem` request to the backend as `Resources`, not just into the PBS header. A PBS host bakes them into `#PBS`, but the SLURM backend takes them as `sbatch` overrides, so before this fix a `--gpus`/`--mem` submit was silently dropped on every SLURM host (the dispatcher always passed an empty `Resources()`).
- `lote why` and `lote wait` now read the scheduler exit code, so a job killed from outside (OOM or walltime, exit 137/143, or a `timeout` deadline exit 124) reads as "killed by SIGKILL (out of memory or walltime)" rather than the misleading last healthy log line of work that did finish. A real Python traceback in the log still wins over the generic signal note.
- `lote status` no longer records two history rows per call. A doubled `@recorded` decorator wrote each invocation to `.lote/db.sqlite` twice.

- Capabilities are now per node class instead of one flat reading per host. The ssh login node is the `login` class, and `discover` enumerates every scheduler queue (`qstat -q` on PBS, `sinfo` on SLURM) and probes each with a minimal submitted job that prints mainboard's machine snapshot, so GPU queues and special classes like Miyabi's `prepost` movers cache their real GPU, memory, and cores under `(host, class)` keys in `.lote/db.sqlite`. `lote ls` lists each class beneath its host, `submit auto` routes by the best class, hardware hints are gone (identity fields stay hintable), and a queue that rejects, fails, or outwaits `--wait` is skipped with a warning. ssh hosts onboard exactly as before.

### Fixed

- `lote interact` no longer drops the interactive queue on clusters whose `qstat` rejects `-Q` (Miyabi). The capability probe now falls back to `qstat --rsc` and picks the interactive parent router (`interact-g`), so `lote interact <host>` builds `qsub -I -q interact-g` instead of an unqualified `qsub -I` that lands on a default queue and is denied. The `_n1` leaf is access-denied for interactive submits; the parent router is the queue `qsub -I` accepts.

## 0.0.2

### Added

- A single common interface to interact with any machine. `lote run <target> "<cmd>"` dispatches through the target's scheduler on every backend (local, ssh, pueue, PBS, SLURM), streams and captures the output, and is cancellable. `--detach` returns a handle and survives disconnect, with the output retrievable later via `lote logs`.
- `lote ps <target>` lists a host's live and queued work uniformly across backends, and `lote cancel`/`kill <target> <handle>` stops it.
- A `lote cancel` verb and a generated-job path for `submit` (the command is positional, quoted like ssh).
- Job scripts render from packaged jinja2 templates, and onboarding installs chefe from the synced source so a host's chefe can never drift from the manifest, with PyPI upgrade as the source-less fallback.
- The pueue client resolves the chefe env's own binary under the host root, with PATH as the fallback.
- A generated job runs the pixi env python directly when chefe is absent or out of date, so a host with a built env but a stale chefe still runs. `setup` now upgrades chefe.
- A clear host-key verification error instead of a raw plumbum traceback.

### Changed

- The CLI moved from fire to cyclopts. Returned values print exactly once through the handled wrapper, and `lote exec run/info/logs/qsub/sbatch` argv shapes are preserved for generated scripts.
- Synchronous `lote run` actually streams. Schedulers poll state and drain new log bytes by offset until the terminal state, then exit with the job's real code, so a failed, killed, or vanished job never exits 0.
- Finished PBS jobs resolve through the `-H` history fallback instead of reporting vanished, and a finished job with no exit status is a distinct `unknown` verdict.
- Python 3.14 is the floor, with native deferred annotations and sqlite `autocommit`.
- Job scripts are content addressed by sha256, SLURM jobs no longer force a GPU request, scheduler dispatch fails fast on an unknown kind through a patos Strategy registry, and typo'd `[hints]` keys are rejected naming the bad key.
- Typing is mypy strict with `disallow_any_explicit`. The source carries no explicit `Any` or `object` annotations.
- The docs adopt the shared Open Props design language over mkdocs-material, with a legible app-icon as logo and favicon, and a working `llms.txt` from the english post-build hook.
