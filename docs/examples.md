# Examples

The fastest tour of lote is a few real workflows. Each one is the same `train.sh` reaching a different machine, with lote adapting underneath.

## Onboard a new host

Point lote at any ssh alias and let it discover the rest.

```sh
lote ls                        # the ssh-config aliases lote can target
lote setup miyabi              # probe + rsync + chefe install, then start the queue
lote discover pegasus          # onboard and print what it is, without starting a queue
```

`setup` finds the repo root, ships the code, builds the env with chefe, and probes the host in a login shell so the cluster toolchain is visible. After this, lote knows miyabi is a slurm host and pegasus is pbs, and every later command routes itself.

## Dispatch and follow a job

The core loop. Ship, launch, watch, pull.

```sh
lote submit miyabi train.sh --fetch research/out/run-42   # prints a handle, e.g. 1837492
lote status miyabi                                        # is it running yet?
lote logs miyabi 1837492 --follow                         # tail the merged output
lote info miyabi 1837492                                  # exit code, memory used, GPU
lote pull 1837492                                         # rsync research/out/run-42 back
```

`submit` records the handle with the git sha it ran, so a result on disk always traces back to the exact commit. Because you passed `--fetch`, `pull` already knows the path and rsyncs it home with no retyping.

## Submit a command, skip the worker script

No `train.sh` to write. Pass the command and lote generates the job script for the host.

```sh
lote submit miyabi --cmd "python -m project.train --model gb10" --gpus 1 --walltime 02:00:00
```

lote renders a `#PBS` script on a PBS host (a plain bash wrapper elsewhere), ships it, and submits it. The generated body sources `.chefe/activate.sh` for the whole environment, falling back to the pixi env's own python when a host has the env built but no current chefe, so the job runs either way. To launch the same command on several hosts at once, add `--targets a,b,c` and lote returns the comma-joined handles.

## Route a job by memory

Let lote choose the machine for a job that needs a known amount of memory.

```sh
lote submit auto train.sh --needs 40      # smallest host with >= 40 GB free, ships and launches
```

lote considers every onboarded host, drops the ones that do not fit 40 GB, and picks the smallest of the rest, so a small job does not occupy a large machine. If nothing fits, it tells you what each host has.

## Iterate live on a remote host

Keep a host in lockstep with your editor while you debug.

```sh
lote watch dgx-spark           # re-rsync on every save, ctrl-c to stop
# in another shell:
lote submit dgx-spark smoke.sh    # the host already has your latest code
```

`watch` honours the same `[sync]` allowlist and gitignore as `submit`, so only the files a compute node needs travel, and only when they change. On a plain box like dgx-spark the job goes to `pueue`, the same `smoke.sh` that would hit `sbatch` on miyabi.

## Track every host

See everything at once, from your laptop, offline.

```sh
lote ps                        # the last runs across every machine, from the state store
lote reconcile miyabi          # recorded runs vs the live scheduler, with a verdict each
lote history                   # the recent lote commands you ran
```

`ps` and `history` read `.lote/db.sqlite`, so they answer instantly without touching a host. `reconcile` opens one ssh connection, asks the scheduler about each recorded run, and labels each ok, failed, running, or vanished, which is how you notice a job that died without an email.

## Run the executor by hand

On a login node you can drive the executor directly, the same code lote calls over ssh.

```sh
lote exec sbatch train.sh --gpus 4         # submit here, return the job id
lote exec status                           # my live jobs, squeue or qstat as fits
lote exec logs train --follow              # tail by job name, not just id
lote exec cancel all                       # stop every job of mine
```

This is useful when you are already on the cluster, or when debugging what lote would run remotely. The directives in `train.sh` set the defaults, and the flags override them.
