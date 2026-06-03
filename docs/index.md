# fleetctl

One command plane for your machines.

A DGX over ssh, a slurm cluster, a pbs supercomputer, the spare box under the desk. Each runs jobs its own way, so the same experiment needs `pueue` here, `sbatch` there, and `qsub` somewhere else. **fleet** is the plane that sits over all of them. You pick a machine, it rsyncs your repo there, runs your job under whatever scheduler that host has, and pulls the results back. One job script runs anywhere.

<div class="grid cards" markdown>

- :material-rocket-launch-outline: **One submit**

    `fleet submit <host> job.sh` ships the repo, launches the job, and prints a handle. No matter the scheduler.

- :material-transit-connection-variant: **Any scheduler**

    fleet detects each host's queue and adapts. `pueue` on a plain box, `sbatch` on slurm, `qsub` on pbs.

- :material-radar: **Fleet-wide state**

    `fleet ps` and `fleet reconcile` track every run across every machine from your laptop.

- :material-lan-connect: **Just ssh**

    Targets are your `~/.ssh/config` host aliases. No agent to install, no control daemon to babysit.

</div>

## Installation

```sh
curl -fsSL https://phvv.me/fleetctl/install.sh | sh
```

fleet dispatches its on-host executor over ssh as `chefe run fleet exec ...`, so each host needs [chefe](https://phvv.me/chefe) (the sibling env tool) installed to build and enter the environment. Prefer the raw package? `pip install fleetctl` (the command stays `fleet`).

```toml title="fleet.toml"
[sync]
include = ["research", "toolbox", "pixi.toml"]   # what a compute node needs
exclude = ["**/.venv", "**/__pycache__", "**/*.ckpt"]   # heavy, regenerable
```

```sh
fleet setup miyabi                 # onboard a host: probe, sync, build the env
fleet submit miyabi train.sh       # ship the repo and launch, prints a handle
fleet ps                           # recent runs across every machine
fleet pull <handle>                # rsync the results back
```

!!! warning "fleet is early (`0.0.x`)"
    The config format and commands may still change.

## Next

<div class="grid cards" markdown>

- [:material-cogs: **How it works**](how-it-works.md) — the dispatch pipeline and its three layers.
- [:material-console: **Commands**](commands.md) — the full CLI.
- [:material-tune: **Configuration**](configuration.md) — `fleet.toml`, ssh targets, and the state store.
- [:material-test-tube: **Examples**](examples.md) — real end-to-end workflows.

</div>

## Lore

A fleet is many ships under one command. Each ship has its own crew and its own rigging, yet they sail as one because the flagship sets the heading and every captain knows how to follow it. Your machines are that fleet. fleet is the flagship, and one order reaches them all. ⛵
