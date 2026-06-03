# lote

One command plane for your machines.

A DGX over ssh, a slurm cluster, a pbs supercomputer, the spare box under the desk. Each runs jobs its own way, so the same experiment needs `pueue` here, `sbatch` there, and `qsub` somewhere else. **lote** is the plane that sits over all of them. You pick a machine, it rsyncs your repo there, runs your job under whatever scheduler that host has, and pulls the results back. One job script runs anywhere.

<div class="grid cards" markdown>

- :material-rocket-launch-outline: **One submit**

    `lote submit <host> job.sh` ships the repo, launches the job, and prints a handle. No matter the scheduler.

- :material-transit-connection-variant: **Any scheduler**

    lote detects each host's queue and adapts. `pueue` on a plain box, `sbatch` on slurm, `qsub` on pbs.

- :material-radar: **State across hosts**

    `lote ps` and `lote reconcile` track every run across every machine from your laptop.

- :material-lan-connect: **Just ssh**

    Targets are your `~/.ssh/config` host aliases. No agent to install, no control daemon to babysit.

</div>

## Installation

```sh
pip install lote      # or: uv tool install lote
```

lote dispatches its on-host executor over ssh as `chefe run lote exec ...`, so each host needs [chefe](https://phvv.me/chefe) (the sibling env tool), which `lote setup` installs and which brings up pixi on first run.

```toml title="lote.toml"
[sync]
include = ["research", "toolbox", "pixi.toml"]   # what a compute node needs
exclude = ["**/.venv", "**/__pycache__", "**/*.ckpt"]   # heavy, regenerable
```

```sh
lote setup miyabi                 # onboard a host: probe, sync, build the env
lote submit miyabi train.sh       # ship the repo and launch, prints a handle
lote ps                           # recent runs across every machine
lote pull <handle>                # rsync the results back
```

!!! warning "lote is early (`0.0.x`)"
    The config format and commands may still change.

## Next

<div class="grid cards" markdown>

- [:material-cogs: **How it works**](how-it-works.md) — the dispatch pipeline and its three layers.
- [:material-console: **Commands**](commands.md) — the full CLI.
- [:material-tune: **Configuration**](configuration.md) — `lote.toml`, ssh targets, and the state store.
- [:material-test-tube: **Examples**](examples.md) — real end-to-end workflows.

</div>
