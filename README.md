<div align="center">

[![CI](https://github.com/phvv-me/fleetctl/actions/workflows/ci.yml/badge.svg)](https://github.com/phvv-me/fleetctl/actions/workflows/ci.yml)
[![Publish](https://github.com/phvv-me/fleetctl/actions/workflows/publish.yml/badge.svg)](https://github.com/phvv-me/fleetctl/actions/workflows/publish.yml)
[![PyPI](https://img.shields.io/pypi/v/fleetctl)](https://pypi.org/project/fleetctl/)
[![Python](https://img.shields.io/pypi/pyversions/fleetctl)](https://pypi.org/project/fleetctl/)
[![Docs](https://img.shields.io/badge/docs-phvv.me%2Ffleetctl-EAB308)](https://phvv.me/fleetctl)
[![Coverage](https://img.shields.io/badge/coverage-99%25-brightgreen)](https://github.com/phvv-me/fleetctl/actions/workflows/ci.yml)

</div>

> **Warning** fleet is early (`0.0.x`). The config and commands may still change.

## Installation

```sh
curl -fsSL https://phvv.me/fleetctl/install.sh | sh
```

Prefer the raw package? Use `pip install fleetctl`. The command is `fleet`.

## What it is

You have a laptop and a handful of machines. A DGX in the corner, a couple of
PCs over ssh, a SLURM cluster you share with a lab, a PBS allocation somewhere
else. Each one runs a different scheduler and expects a different incantation.
fleet is one bridge over all of them. You pick a machine, fleet rsyncs your repo
there, runs your job under whatever scheduler that host has, and pulls the
results back. One job script runs anywhere, no code changes.

It works in three layers. The control plane `fleet` onboards hosts, dispatches
jobs, and tracks them across the whole fleet. The on-host executor `fleet exec`
runs, submits, and monitors a single job on one machine. Underneath are typed
clients that wrap `qsub`, `sbatch`, `pueue`, and `rsync`. fleet drives the
executor over ssh as `chefe run fleet exec ...`, so it pairs with
[chefe](https://phvv.me/chefe), the sibling tool that builds the env on each
host.

Targets come from your `~/.ssh/config` aliases, so there is nothing new to
declare. Config is a tiny `fleet.toml` that is essentially just the rsync scope.
Run state lives in one TinyDB file at `.fleet/db.json`.

```toml
[sync]
include = ["src", "research", "pixi.toml"]   # what a compute node needs
exclude = ["**/__pycache__", "**/*.ckpt"]     # heavy or regenerable, skip it
```

## Usage

```sh
fleet ls                       # ssh targets with their cached capabilities
fleet setup dgx                # onboard: probe, sync, install the env, start the queue
fleet submit dgx train.sh      # ship the repo and launch the job, get a handle back
fleet pull <handle>            # rsync the run's recorded results back home
```

Routing, monitoring, and cleanup come from the same bridge.

```sh
fleet submit auto train.sh --needs 40        # route to the smallest host that fits
fleet ps                                      # recent runs across every target
fleet status cluster                          # live jobs on a host
fleet logs cluster <handle> --follow          # tail a run as it goes
fleet reconcile cluster                       # local run state vs the live scheduler
```

> **Tip** run `fleet reconcile <target>` anytime to see each run's live state,
> exit code, and a verdict (ok, failed, running, vanished) without digging
> through email.

The full command set is `ls discover setup submit ps status reconcile interact
logs info fetch pull watch history`, plus the `exec` group (`run submit qsub
sbatch status info logs cancel`) for driving a single host directly.

## Lore

A fleet answers to one bridge. You set the heading and the ships sail, each
crew working the rig their hull was built for. `fleet` does the same for your
machines. You give the order from one place, and every host runs the job the
way it knows how.
