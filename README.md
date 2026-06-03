<div align="center">

[![lote banner](https://raw.githubusercontent.com/phvv-me/lote/main/docs/assets/banner.png)](https://phvv.me/lote)

[![CI](https://github.com/phvv-me/lote/actions/workflows/ci.yml/badge.svg)](https://github.com/phvv-me/lote/actions/workflows/ci.yml)
[![Docs](https://img.shields.io/badge/docs-phvv.me%2Flote-7c3aed)](https://phvv.me/lote)
[![Coverage](https://img.shields.io/badge/coverage-99%25-brightgreen)](https://github.com/phvv-me/lote/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue)](LICENSE)

</div>

> **Warning** lote is early (`0.0.x`). The config and commands may still change.

## Installation

```sh
curl -fsSL https://phvv.me/lote/install.sh | sh
```

The command is `lote`.

## What it is

You have a laptop and a handful of machines, each one running a different
scheduler and expecting a different incantation. lote is one bridge over all of
them. You pick a host, lote rsyncs your repo there, runs your job under whatever
scheduler that host has (pueue, slurm, pbs, or bare bash), and pulls the results
back. One job script runs anywhere, no code changes.

It works in three layers. The control plane `lote` onboards hosts, dispatches
jobs, and tracks them across your machines. The on-host executor `lote exec`
runs, submits, and monitors a single job on one machine. Underneath are typed
clients over `qsub`, `sbatch`, `pueue`, and `rsync`. Targets come from your
`~/.ssh/config` aliases, so there is nothing new to declare, and it pairs with
[chefe](https://phvv.me/chefe) to build the env on each host.

```sh
lote setup dgx                # onboard: probe, sync, install the env, start the queue
lote submit dgx train.sh      # ship the repo, launch the job, get a handle back
lote submit auto train.sh --needs 40   # or route to the smallest host that fits
lote ps                       # recent runs across every target
lote pull <handle>            # rsync the run's results back home
```

> **Tip** `lote reconcile <target>` shows each run's live state, exit code, and a
> verdict (ok, failed, running, vanished). The [docs](https://phvv.me/lote)
> cover the full command set.
