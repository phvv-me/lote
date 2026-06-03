# Examples

The fastest tour of fleet is a few real workflows. Each one is the same `train.sh` reaching a different machine, with fleet adapting underneath.

## Onboard a new host

Point fleet at any ssh alias and let it discover the rest.

```sh
fleet ls                       # the ssh-config aliases fleet can target
fleet setup miyabi             # probe + rsync + chefe install, then start the queue
fleet discover pegasus         # onboard and print what it is, without starting a queue
```

`setup` finds the repo root, ships the code, builds the env with chefe, and probes the host in a login shell so the cluster toolchain is visible. After this, fleet knows miyabi is a slurm host and pegasus is pbs, and every later command routes itself.

## Dispatch and follow a job

The core loop. Ship, launch, watch, pull.

```sh
fleet submit miyabi train.sh --fetch research/out/run-42   # prints a handle, e.g. 1837492
fleet status miyabi                                        # is it running yet?
fleet logs miyabi 1837492 --follow                         # tail the merged output
fleet info miyabi 1837492                                  # exit code, memory used, GPU
fleet pull 1837492                                         # rsync research/out/run-42 back
```

`submit` records the handle with the git sha it ran, so a result on disk always traces back to the exact commit. Because you passed `--fetch`, `pull` already knows the path and rsyncs it home with no retyping.

## Route a job by memory

Let fleet choose the machine for a job that needs a known amount of memory.

```sh
fleet submit auto train.sh --needs 40      # smallest host with >= 40 GB free, ships and launches
```

fleet considers every onboarded host, drops the ones that do not fit 40 GB, and picks the smallest of the rest, so a small job does not occupy a large machine. If nothing fits, it tells you what each host has.

## Iterate live on a remote host

Keep a host in lockstep with your editor while you debug.

```sh
fleet watch dgx-spark          # re-rsync on every save, ctrl-c to stop
# in another shell:
fleet submit dgx-spark smoke.sh   # the host already has your latest code
```

`watch` honours the same `[sync]` allowlist and gitignore as `submit`, so only the files a compute node needs travel, and only when they change. On a plain box like dgx-spark the job goes to `pueue`, the same `smoke.sh` that would hit `sbatch` on miyabi.

## Track the whole fleet

See everything at once, from your laptop, offline.

```sh
fleet ps                       # the last runs across every machine, from the state store
fleet reconcile miyabi         # recorded runs vs the live scheduler, with a verdict each
fleet history                  # the recent fleet commands you ran
```

`ps` and `history` read `.fleet/db.json`, so they answer instantly without touching a host. `reconcile` opens one ssh connection, asks the scheduler about each recorded run, and labels each ok, failed, running, or vanished, which is how you notice a job that died without an email.

## Run the executor by hand

On a login node you can drive the executor directly, the same code fleet calls over ssh.

```sh
fleet exec sbatch train.sh --gpus 4        # submit here, return the job id
fleet exec status                          # my live jobs, squeue or qstat as fits
fleet exec logs train --follow             # tail by job name, not just id
fleet exec cancel all                      # stop every job of mine
```

This is useful when you are already on the cluster, or when debugging what fleet would run remotely. The directives in `train.sh` set the defaults, and the flags override them.
