# PBS clusters

PBS (`qsub`/`qstat`) drives most academic HPC, including Fugaku-class and
GH200 machines. lote treats a PBS host like any other target. You `lote submit`
a job script and lote runs it through the host's `lote exec qsub` in a login
shell. This page covers the few PBS facts that bite if you forget them, all of
which lote now guards or documents.

## The job script contract

A PBS batch job does **not** start where you submitted it and does **not**
inherit your activated environment. Two lines belong at the top of every job
script.

```bash
#!/bin/bash
#PBS -q short-g
#PBS -l select=1
#PBS -l walltime=02:00:00
#PBS -j oe
set -euo pipefail
cd "${PBS_O_WORKDIR:-$HOME}"          # PBS starts the job in $HOME, not the repo
export PYTHONPATH="$PWD/research:$PWD:${PYTHONPATH:-}"
chefe run python experiments/my_run.py   # chefe run, so the env python is active
```

Two mistakes account for almost every silent failure.

1. **No `cd`.** PBS launches the job in `$HOME`. A script that runs
   `python research/...` with repo-relative paths dies instantly because those
   paths do not exist under `$HOME`. `cd "$PBS_O_WORKDIR"` puts you back where
   you submitted. The job then prints nothing useful and finishes in seconds,
   which reads like a crash with no cause.
2. **Bare `python`.** A fresh compute-node shell has none of your project env on
   PATH. Always go through `chefe run`, which activates the env first. lote
   itself reaches the host this way, but your job script is on its own once PBS
   hands it a clean shell.

## Queues: submit to a leaf, not a router

Many PBS sites split a logical queue into a routing parent and several execution
leaves. `qsub -q regular-g` may be rejected because `regular-g` is a routing
queue, while `short-g` and `debug-g` are the leaves that actually run work. List
them and their limits with

```sh
qstat -Q        # one line per queue
qstat -Qf       # full attributes, including max walltime and node count
```

Pick a leaf your project is allowed to use. Access is controlled per project
(the `Qlist`/`acl` attributes), so a queue that exists is not always a queue you
can submit to. For fast iteration prefer `debug-g` (it schedules in seconds, with
a short walltime cap). Reserve `short-g` and larger queues for real runs.

## Monitoring: a queued job is invisible to `-H`

This is the single most confusing PBS behavior. `qstat -H <id>` reports
**history**, meaning finished jobs only. A job still sitting in the queue, or
running, appears in neither the default filtered view nor `-H`, so it looks like
it vanished. It did not. Use the plain form.

```sh
qstat <id>      # queued (Q), running (R), or held
qstat           # everything of yours that is not yet finished
qstat -H <id>   # only after it ends
```

A backlogged queue can hold a job for hours. `qstat <id>` shows the estimated
start time in its comment. Output files (`-o`/`-e`) are written at job end, so an
empty log directory while a job is queued is normal, not a failure.

## File ownership and quota

On shared project storage, files a job writes must carry the project group or
they count against your personal quota and may be unreadable by collaborators.
Pass the project as the group list and let the storage `setgid` bit or
`newgrp` handle the rest.

```sh
#PBS -W group_list=<project>
```

lote derives this automatically from a `/work/<project>/` style root when it can,
so you rarely set it by hand.

## What lote guarantees

- `lote submit <host> <script.sh>` ships the repo, runs `qsub` in a login shell,
  and returns the job id. If `qsub` fails (a routing queue, a quota reject, an
  access denial), lote raises with the stderr instead of handing back a blank
  handle, so a failed submit can never look like a successful one.
- **Every submitted job is wrapped** so it `cd`s into the submit directory and
  has the user install dirs on PATH before your script runs. A script that forgets
  `cd "$PBS_O_WORKDIR"` or uses a bare `python` is rescued, not silently failed in
  `$HOME`. The wrapper still `exec`s your script unchanged, so one that already
  cds/activates is unaffected. You remain responsible only for running interpreters
  through `chefe run` (the wrapper makes `chefe` findable, but it does not auto-wrap
  your commands).
- `lote status <host>` and `lote info <host> <id>` reconcile the job against the
  live scheduler. `lote logs <host> <id> --follow` tails the output once it
  exists, and `lote cancel <host> <id>` runs the host's `qdel` to stop it.
- `lote submit <host> --cmd "<command>"` skips the job script entirely. lote
  renders the `#PBS` header from `--queue`, `--walltime`, and `--gpus`, then a body
  that sources `.chefe/activate.sh` (falling back to the pixi env's python when
  chefe is absent), so you never hand-write a `worker.sh`.

## Quick checklist

- [ ] `cd "$PBS_O_WORKDIR"` is the first real line of the job script.
- [ ] Every interpreter call goes through `chefe run`.
- [ ] `#PBS -q` names a leaf queue your project can use.
- [ ] You check status with `qstat <id>`, not `qstat -H <id>`.
- [ ] `#PBS -W group_list=<project>` is set on shared project storage.
