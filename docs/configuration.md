# Configuration

lote reads three things. `lote.toml` says what to ship, `~/.ssh/config` says where, and `.lote/db.sqlite` remembers what happened. Everything else about a host is auto-discovered when you onboard it, so most setups never touch more than the `[sync]` table.

## lote.toml

The repo-root `lote.toml` is essentially just the rsync scope. `include` is the allowlist of paths a compute node needs, and `exclude` skips heavy or regenerable patterns inside them.

```toml title="lote.toml"
[sync]
include = ["research", "toolbox", "pixi.toml", "chefe.toml"]   # what to ship
exclude = [                                                    # what to skip within them
  "**/.venv",
  "**/__pycache__",
  "**/*.ckpt",
  "**/wandb",
]
```

`include` paths are preserved with `rsync -R`, so the tree lands at the same place on the host. `exclude` patterns stack on top of your gitignore, which lote already honours, so you only list the extra heavy artifacts here. `lote watch` watches the same `include` set and re-syncs when any non-ignored file in it changes.

Sync mirrors the local tree. Anything inside the `include` set that no longer exists locally is pruned on the host (`rsync --delete`), so a renamed module never leaves a stale copy behind shadowing its replacement. Host-only artifacts survive through `protect`, a list of rsync filter patterns whose defaults shield `.lote/***`, `results/***`, `logs/***` and `*.log` (the `dir/***` form matches a directory and everything inside it, at any depth). Excluded paths are never deleted either. Setting `protect` replaces the defaults, so repeat them when you only mean to add:

```toml
[sync]
protect = [".lote/***", "results/***", "logs/***", "*.log", "checkpoints/***"]
```
On macOS install upstream rsync (`brew install rsync`), since Apple's bundled openrsync cannot prune on a remote host and lote refuses to mirror with it.

### Targets and hints

Both are optional power-user overrides. By default targets come from `~/.ssh/config` and every capability is probed, so you rarely set these.

```toml
targets = ["miyabi", "pegasus", "dgx-spark"]   # restrict to these aliases

[hints.dgx-spark]                              # override a probed field
root = "/data/projects"
```

A `targets` list narrows lote to specific aliases instead of every host in your ssh config. A `[hints.<alias>]` table overrides a probed field for one host, its keys being the target's identity fields (`root`, `kind`, `account`, `queue`, `login_shell`). Hardware capabilities are not hintable because they are probed per node class, as the next section explains.

## ssh targets

lote's targets are the concrete `Host` aliases in `~/.ssh/config`. Pattern entries like `Host *` are ignored, leaving the real, connectable destinations.

```ssh-config title="~/.ssh/config"
Host dgx-spark
    HostName 192.168.1.40
    User pedro

Host miyabi
    HostName miyabi.cc.example.ac.jp
    User pedro
    ProxyJump gateway

Host pegasus
    HostName pegasus.example.ac.jp
    User pedro
```

Because targets are ssh aliases, everything you already configure for ssh just works, including `ProxyJump`, identity files, and connection multiplexing. lote reuses one connection per command. There is no agent to install on the host and no control daemon to run, only ssh and, on each host, [chefe](https://phvv.me/chefe) so the executor can build and enter the env.

## Node classes

A host is rarely one machine. The ssh login node is one class of node, and each scheduler queue (a PBS queue, a SLURM partition) fronts its own hardware, so an HPC host's GPU queues look nothing like its login node and special queues like Miyabi's `prepost` data movers are different again. lote therefore keeps capabilities per node class.

The `login` class comes from the stock over-ssh probe, with nothing installed on the host. Every other class is discovered during onboarding by asking the scheduler for its queue list (`qstat -q` on PBS, `sinfo` on SLURM) and submitting one minimal job to each queue, whose whole payload is printing [mainboard](https://phvv.me/mainboard)'s machine snapshot. The parsed snapshot becomes that queue's class, cached under its class key as the probe jobs report back, and `lote ls` lists each class beneath its host. A queue that rejects the job, fails it, or sits busy past `--wait` (600 seconds by default) is skipped with a warning, never a failed discover. An ssh host has no queues, so its onboarding stays the single login probe it always was.

## .lote/db.sqlite

Run state lives in one WAL-mode [SQLite](https://www.sqlite.org) file at `.lote/db.sqlite`. SQLite gives concurrent-safe upserts with no whole-file rewrite, so parallel `lote` calls never corrupt the store. It holds four things, all written from your laptop.

| store | what it remembers |
|---|---|
| host facts | each onboarded host's probed scheduler, repo root, and account |
| node classes | one row per `(host, class)` key, the probed GPU, memory, and cores of that class |
| run registry | every dispatch, with its handle, target, script, git sha, and `--fetch` path |
| command history | each `lote` invocation, timed, with its result |

`lote ls` reads the cached host facts and classes without probing again. `lote ps` and `lote pull` read the run registry, so they work offline. `lote history` reads the command log, and a sibling `.lote/lote.log` keeps a rotated text trace. Set `LOTE_NO_HISTORY=1` to skip recording. Onboarding writes a host's facts only after `chefe install` succeeds, so the registry only ever names machines that can actually run your jobs.
