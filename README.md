<div align="center">

[![lote banner](https://raw.githubusercontent.com/phvv-me/lote/main/docs/assets/banner.png)](https://phvv.me/lote)

[![CI](https://github.com/phvv-me/lote/actions/workflows/ci.yml/badge.svg)](https://github.com/phvv-me/lote/actions/workflows/ci.yml)
[![Publish](https://github.com/phvv-me/lote/actions/workflows/publish.yml/badge.svg)](https://github.com/phvv-me/lote/actions/workflows/publish.yml)
[![PyPI](https://img.shields.io/pypi/v/lote)](https://pypi.org/project/lote/)
[![Python](https://img.shields.io/pypi/pyversions/lote)](https://pypi.org/project/lote/)
[![Docs](https://img.shields.io/badge/docs-phvv.me%2Flote-7c3aed)](https://phvv.me/lote)
[![Coverage](https://img.shields.io/badge/coverage-100%25-brightgreen)](https://github.com/phvv-me/lote/actions/workflows/ci.yml)

</div>

> **Warning** lote is early (`0.0.x`). The config and commands may still change.

## Installation

```sh
pip install lote            # the lote command, ready to use
chefe add lote --pypi       # or pull it into a chefe project
```

The command is `lote`. It drives each host through [chefe](https://phvv.me/chefe), which `lote setup` installs for you and which brings up pixi on first run.

## What it is

You have a laptop and a handful of machines, each one running a different scheduler and expecting a different incantation. lote is one bridge over all of them. You pick a host, lote rsyncs your repo there, runs your job under whatever scheduler that host has (pueue, slurm, pbs, or bare bash), and pulls the results back. **One job script runs anywhere, no code changes.**

It works in three layers. The control plane `lote` onboards hosts, dispatches jobs, and tracks them across your machines. The on-host executor `lote exec` runs, submits, and monitors a single job on one machine. Underneath are typed clients over `qsub`, `sbatch`, `pueue`, and `rsync`. Targets come from your `~/.ssh/config` aliases, so there is nothing new to declare.

## Usage

```sh
lote setup dgx                          # onboard: probe, sync, install the env, start the queue
lote run dgx "python train.py"          # queue the command, stream its output, exit with its code
lote run dgx "python train.py" --detach # same, but return a handle and survive disconnect
lote submit dgx train.sh                # ship the repo, launch the job, get a handle back
lote submit auto train.sh --needs 40    # or route to the smallest host that fits
lote ps dgx                             # one host's live jobs (any scheduler), uniformly
lote logs dgx <handle>                  # the captured output of any run, even after it ends
lote cancel dgx <handle>                # stop a job on any backend (kill is an alias)
lote ps                                 # recent runs across every target
lote pull <handle>                      # rsync the run's results back home
```

> **Tip** `lote run` and `lote submit` dispatch through the same scheduler, so `ps`, `logs`, and `cancel` work the same way on a laptop, a bare ssh host, or a PBS/SLURM cluster. `lote reconcile <target>` shows each run's live state, exit code, and a verdict (ok, failed, running, vanished). The [docs](https://phvv.me/lote) cover the full command set.

## Lore

A lote is a load shipped as one consignment. lote ships your work the same way: pick a machine, send the job, bring the results home.
</content>
</invoke>
