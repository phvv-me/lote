<div align="center">

[![fleet banner](https://raw.githubusercontent.com/phvv-me/fleetctl/main/docs/assets/banner.png)](https://phvv.me/fleetctl)

[![CI](https://github.com/phvv-me/fleetctl/actions/workflows/ci.yml/badge.svg)](https://github.com/phvv-me/fleetctl/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-phvv.me%2Ffleetctl-EAB308)](https://phvv.me/fleetctl)
[![Coverage](https://img.shields.io/badge/coverage-99%25-brightgreen)](https://github.com/phvv-me/fleetctl/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

</div>

> **Warning** fleet is early (`0.0.x`). The config and commands may still change.

## Installation

```sh
curl -fsSL https://phvv.me/fleetctl/install.sh | sh
```

Prefer the raw package? `pip install fleetctl`. The command is `fleet`.

## What it is

You have a laptop and a handful of machines, each one running a different
scheduler and expecting a different incantation. fleet is one bridge over all of
them. You pick a host, fleet rsyncs your repo there, runs your job under whatever
scheduler that host has (pueue, SLURM, PBS, or bare bash), and pulls the results
back. One job script runs anywhere, no code changes.

It works in three layers. The control plane `fleet` onboards hosts, dispatches
jobs, and tracks them across the fleet. The on-host executor `fleet exec` runs,
submits, and monitors a single job on one machine. Underneath are typed clients
over `qsub`, `sbatch`, `pueue`, and `rsync`. Targets come from your
`~/.ssh/config` aliases, so there is nothing new to declare, and it pairs with
[chefe](https://phvv.me/chefe) to build the env on each host.

```sh
fleet setup dgx                # onboard: probe, sync, install the env, start the queue
fleet submit dgx train.sh      # ship the repo, launch the job, get a handle back
fleet submit auto train.sh --needs 40   # or route to the smallest host that fits
fleet ps                       # recent runs across every target
fleet pull <handle>            # rsync the run's results back home
```

> **Tip** `fleet reconcile <target>` shows each run's live state, exit code, and a
> verdict (ok, failed, running, vanished). The [docs](https://phvv.me/fleetctl)
> cover the full command set.

## Lore

A fleet answers to one bridge. You set the heading and the ships sail, each crew
working the rig their hull was built for. `fleet` does the same for your
machines. You give the order from one place, and every host runs the job the way
it knows how.
