# Changelog

All notable changes to lote are documented here.

The format follows Keep a Changelog, and releases are cut from the version in `pyproject.toml`.

## 0.0.2

### Added

- A single common interface to interact with any machine. `lote run <target> "<cmd>"` dispatches through the target's scheduler on every backend (local, ssh, pueue, PBS, SLURM), streams and captures the output, and is cancellable. `--detach` returns a handle and survives disconnect, with the output retrievable later via `lote logs`.
- `lote ps <target>` lists a host's live and queued work uniformly across backends, and `lote cancel`/`kill <target> <handle>` stops it.
- A `lote cancel` verb and a `--cmd` generated-job path for `submit`.
- A generated job runs the pixi env python directly when chefe is absent or out of date, so a host with a built env but a stale chefe still runs. `setup` now upgrades chefe.
- A clear host-key verification error instead of a raw plumbum traceback.

### Changed

- Typing is mypy strict with `disallow_any_explicit`. The source carries no explicit `Any` or `object` annotations.
- The docs adopt the shared Open Props design language over mkdocs-material, with a legible app-icon as logo and favicon, and a working `llms.txt` from the english post-build hook.
