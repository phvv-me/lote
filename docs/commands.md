# Commands

fleet has two command groups. The top-level `fleet` verbs are the control plane you run on your laptop, and the `fleet exec` group is the on-host executor that runs on a single machine. Targets are `~/.ssh/config` host aliases throughout.

## Control plane

| command | what it does |
|---|---|
| `fleet ls` | list ssh-config targets with their cached capabilities (never probes) |
| `fleet discover <target>` | onboard a host, then show what it is (probe + sync + `chefe install`) |
| `fleet setup <target>` | onboard a host and start its queue daemon |
| `fleet submit <target\|auto> <script> [args…]` | rsync the repo up and launch `script`, prints a handle |
| `fleet ps [limit]` | recent dispatched runs across every target |
| `fleet status <target>` | live jobs on a target |
| `fleet reconcile <target>` | compare recorded runs for a target with the live scheduler |
| `fleet interact <target>` | grab an interactive session (a real TTY) |
| `fleet logs <target> <handle>` | tail a run's log |
| `fleet info <target> <handle>` | a job's post-mortem (exit code, memory, GPU) |
| `fleet fetch <target> <path>` | rsync a results path back |
| `fleet pull <handle>` | rsync back the results path recorded at submit time |
| `fleet watch <target>` | re-sync the repo on every local file change |
| `fleet history [limit]` | recent `fleet` command invocations |

## setup and discover

```sh
fleet discover miyabi          # onboard, then print the host's kind, root, and GPU
fleet setup miyabi             # same onboarding, and start the pueue daemon
```

Onboarding finds the host's repo root (an HPC `/work` area if there is one, else `~/projects`), rsyncs the repo there, runs `chefe install`, then probes the host in a login shell for its scheduler, GPU, and account. A host is cached only after `chefe install` succeeds, so one that cannot build the env never enters the fleet.

## submit

```sh
fleet submit miyabi train.sh                       # to a named host
fleet submit miyabi train.sh --fetch results/run1  # remember a results path for `pull`
fleet submit auto train.sh --needs 40              # route by free memory, pick smallest fit
```

`submit` rsyncs the repo, picks the target's scheduler, and launches the script, recording the git sha (and a dirty flag) alongside the handle. With `auto`, `--needs <GB>` is required and fleet chooses the smallest-memory host that still fits, keeping the big machines free. Extra positional args after the script reach the job through the `ARGS` env var.

## ps, status, and reconcile

```sh
fleet ps                       # the last runs across the whole fleet
fleet status miyabi            # what is live on one host right now
fleet reconcile miyabi         # recorded runs vs the live scheduler
```

`ps` reads the local state store, so it works offline. `status` opens an ssh connection and asks the host's scheduler directly. `reconcile` joins the two, giving each recorded run a live state, exit code, and a one-word verdict (ok, failed, running, or vanished). This is the local-state debugging aid that replaces refreshing a queue by hand.

## logs, info, fetch, and pull

```sh
fleet logs miyabi <handle> --follow    # tail the merged stdout+stderr
fleet info miyabi <handle>             # exit status, memory used vs cap, GPU usage
fleet fetch miyabi research/out        # pull an arbitrary path back
fleet pull <handle>                    # pull the path recorded at submit time
```

`fetch` takes a path relative to the repo root and rsyncs it back into the same local path. `pull` is the shortcut when you passed `--fetch` at submit time, so you do not retype the path.

## interact and watch

```sh
fleet interact miyabi --gpus 2 --hours 4   # interactive session (qsub -I on pbs, ssh -t otherwise)
fleet interact miyabi --dry-run            # print the command instead of running it
fleet watch miyabi                         # re-rsync on every local file change, ctrl-c to stop
```

On a pbs host, `interact` submits an interactive `qsub -I` with the discovered account and queue. On any other host it opens a login shell. `watch` keeps a host in sync with your working tree while you iterate, honouring the same allowlist and gitignore as `submit`.

## exec

The on-host executor. fleet calls it over ssh, but you can also run it directly on a login node.

| command | what it does |
|---|---|
| `fleet exec qsub <script> [args…]` | submit to pbs, return the job id |
| `fleet exec sbatch <script> [args…]` | submit to slurm, return the job id |
| `fleet exec run <script> [args…]` | run through bash, no scheduler |
| `fleet exec status` | a table of my live jobs |
| `fleet exec info <jid>` | a job's post-mortem record |
| `fleet exec logs <jid\|name> [--follow]` | tail the latest matching log |
| `fleet exec cancel <jid\|name\|all>` | `qdel` or `scancel` |

```sh
fleet exec qsub train.sh --select 2 --walltime 04:00:00
fleet exec sbatch train.sh --gpus 4 --partition gpu
fleet exec run smoke.sh                 # plain bash, off a cluster
fleet exec status                       # squeue --me on slurm, otherwise qstat
fleet exec cancel all                   # every job of mine
```

`qsub` and `sbatch` read the script's `#PBS` or `#SBATCH` directives and let flags override them, so one script carries sensible defaults. A bare script name is resolved against the experiment `jobs/` directories, and `status`, `logs`, and `info` pick the host's scheduler automatically.
