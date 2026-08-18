# Commands

lote has two command groups. The top-level `lote` verbs are the control plane you run on your laptop, and the `lote exec` group is the on-host executor that runs on a single machine. Targets are `~/.ssh/config` host aliases throughout.

## Control plane

| command | what it does |
|---|---|
| `lote ls` | list ssh-config targets with their cached capabilities (never probes) |
| `lote probe <target>` | preview a host's live capabilities over ssh, without syncing or installing |
| `lote discover <target>` | onboard a host and map its node classes (probe + sync + `chefe install` + one probe job per queue) |
| `lote setup <target>` | onboard a host and start its queue daemon |
| `lote submit <target\|auto> <script> [args…]` | rsync the repo up and launch `script`, prints a handle |
| `lote ps [limit]` | recent dispatched runs across every target |
| `lote status <target>` | live jobs on a target |
| `lote reconcile <target>` | compare recorded runs for a target with the live scheduler |
| `lote interact <target>` | grab an interactive session (a real TTY) |
| `lote logs <target> <handle>` | tail a run's log |
| `lote cancel <target> <handle>` | cancel a job (`qdel` / `scancel` / pueue kill) |
| `lote info <target> <handle>` | a job's post-mortem (exit code, memory, GPU) |
| `lote fetch <target> <path>` | rsync a results path back |
| `lote pull <handle>` | rsync back the results path recorded at submit time |
| `lote watch <target>` | re-sync the repo on every local file change |
| `lote history [limit]` | recent `lote` command invocations |

## setup and discover

```sh
lote discover miyabi          # onboard, then print the host's kind, root, and node classes
lote setup miyabi             # same onboarding, and start the pueue daemon
lote discover miyabi --wait 120   # give each queue's probe job at most two minutes
```

Onboarding finds the host's repo root (an HPC `/work` area if there is one, else `~/projects`), rsyncs the repo there, makes sure chefe is installed and current, resolves the deliberately synced manifest through `chefe install --resolve`, then probes the host in a login shell for its scheduler, GPU, and account. Chefe follows that solve with its own verified locked pass. A host is cached only after installation succeeds, so one that cannot build the env never becomes a target.

On a scheduler host, discovery then maps the node classes. It asks the scheduler for its queues (`qstat -q` on PBS, `sinfo` on SLURM) and submits one minimal job per queue that prints [mainboard](https://phvv.me/mainboard)'s machine snapshot, so each queue's real GPU, memory, and cores are cached under that class key, including special classes like Miyabi's `prepost` movers. A queue that rejects the job or stays busy past `--wait` is skipped with a warning and picked up by the next discover.

`probe` is the read-only preview: it asks a host what it is over ssh without syncing, installing, or caching anything.

## submit

```sh
lote submit miyabi train.sh                          # run an existing script on a named host
lote submit miyabi --cmd "python -m foo --model X"   # generate the job script from a command
lote submit miyabi train.sh --fetch results/run1     # remember a results path for `pull`
lote submit auto train.sh --needs 40                 # route by free memory, pick smallest fit
lote submit any --targets a,b,c train.sh             # fan the same job across several hosts
```

`submit` rsyncs the repo, picks the target's scheduler, and launches the script, recording the git sha (and a dirty flag) alongside the handle. With `auto`, `--needs <GB>` is required and lote chooses the smallest-memory host that still fits, keeping the big machines free. Extra positional args after the script reach the job through the `ARGS` env var.

Instead of writing a `worker.sh` per experiment, pass `--cmd` and lote generates the job script for the host's scheduler (a `#PBS` header from `--queue`, `--walltime`, and `--gpus` on a PBS host, a plain bash wrapper elsewhere). The generated body sources `.chefe/activate.sh` for the whole environment when chefe wrote one. If that file is absent, it runs the pixi env's own python directly, so a host with a built env but a missing or out-of-date chefe still runs with no chefe invoked at job time. `--targets a,b,c` fans the same job across several hosts in one call and returns the comma-joined handles (the positional target is then a placeholder).

## ps, status, and reconcile

```sh
lote ps                       # the last runs across every machine
lote status miyabi            # what is live on one host right now
lote reconcile miyabi         # recorded runs vs the live scheduler
```

`ps` reads the local state store, so it works offline. `status` opens an ssh connection and asks the host's scheduler directly. `reconcile` joins the two, giving each recorded run a live state, exit code, and a one-word verdict (ok, failed, running, or vanished). This is the local-state debugging aid that replaces refreshing a queue by hand.

## logs, info, fetch, and pull

```sh
lote logs miyabi <handle> --follow    # tail the merged stdout+stderr
lote info miyabi <handle>             # exit status, memory used vs cap, GPU usage
lote cancel miyabi <handle>           # qdel / scancel / pueue kill, picked per host
lote fetch miyabi research/out        # pull an arbitrary path back
lote pull <handle>                    # pull the path recorded at submit time
```

`fetch` takes a path relative to the repo root and rsyncs it back into the same local path. `pull` is the shortcut when you passed `--fetch` at submit time, so you do not retype the path. `cancel` stops a live job, choosing the host's own kill verb (`qdel` on PBS, `scancel` on Slurm, a pueue kill otherwise).

## interact and watch

```sh
lote interact miyabi --gpus 2 --hours 4   # interactive session (qsub -I on pbs, ssh -t otherwise)
lote interact miyabi --dry-run            # print the command instead of running it
lote watch miyabi                         # re-rsync on every local file change, ctrl-c to stop
```

On a pbs host, `interact` submits an interactive `qsub -I` with the discovered account and queue. On any other host it opens a login shell. `watch` keeps a host in sync with your working tree while you iterate, honouring the same allowlist and gitignore as `submit`.

## exec

The on-host executor. lote calls it over ssh, but you can also run it directly on a login node.

| command | what it does |
|---|---|
| `lote exec qsub <script> [args…]` | submit to pbs, return the job id |
| `lote exec sbatch <script> [args…]` | submit to slurm, return the job id |
| `lote exec run <script> [args…]` | run through bash, no scheduler |
| `lote exec status` | a table of my live jobs |
| `lote exec info <jid>` | a job's post-mortem record |
| `lote exec logs <jid\|name> [--follow]` | tail the latest matching log |
| `lote exec cancel <jid\|name\|all>` | `qdel` or `scancel` |

```sh
lote exec qsub train.sh --select 2 --walltime 04:00:00
lote exec sbatch train.sh --gpus 4 --partition gpu
lote exec run smoke.sh                 # plain bash, off a cluster
lote exec status                       # squeue --me on slurm, otherwise qstat
lote exec cancel all                   # every job of mine
```

`qsub` and `sbatch` read the script's `#PBS` or `#SBATCH` directives and let flags override them, so one script carries sensible defaults. A bare script name is resolved against the experiment `jobs/` directories, and `status`, `logs`, and `info` pick the host's scheduler automatically.
