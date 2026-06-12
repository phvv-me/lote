# Changelog

All notable changes to lote are documented here.

The format follows Keep a Changelog, and releases are cut from the version in `pyproject.toml`.

## Unreleased

### Changed

- Capabilities are now per node class instead of one flat reading per host. The ssh login node is the `login` class, and `discover` enumerates every scheduler queue (`qstat -q` on PBS, `sinfo` on SLURM) and probes each with a minimal submitted job that prints mainboard's machine snapshot, so GPU queues and special classes like Miyabi's `prepost` movers cache their real GPU, memory, and cores under `(host, class)` keys in `.lote/db.sqlite`. `lote ls` lists each class beneath its host, `submit auto` routes by the best class, hardware hints are gone (identity fields stay hintable), and a queue that rejects, fails, or outwaits `--wait` is skipped with a warning. ssh hosts onboard exactly as before.

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
