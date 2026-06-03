# Configuration

fleet reads three things. `fleet.toml` says what to ship, `~/.ssh/config` says where, and `.fleet/db.json` remembers what happened. Everything else about a host is auto-discovered when you onboard it, so most setups never touch more than the `[sync]` table.

## fleet.toml

The repo-root `fleet.toml` is essentially just the rsync scope. `include` is the allowlist of paths a compute node needs, and `exclude` skips heavy or regenerable patterns inside them.

```toml title="fleet.toml"
[sync]
include = ["research", "toolbox", "pixi.toml", "chefe.toml"]   # what to ship
exclude = [                                                    # what to skip within them
  "**/.venv",
  "**/__pycache__",
  "**/*.ckpt",
  "**/wandb",
]
```

`include` paths are preserved with `rsync -R`, so the tree lands at the same place on the host. `exclude` patterns stack on top of your gitignore, which fleet already honours, so you only list the extra heavy artifacts here. `fleet watch` watches the same `include` set and re-syncs when any non-ignored file in it changes.

### Targets and hints

Both are optional power-user overrides. By default targets come from `~/.ssh/config` and every capability is probed, so you rarely set these.

```toml
targets = ["miyabi", "pegasus", "dgx-spark"]   # restrict the fleet to these aliases

[hints.dgx-spark]                              # override a probed field
root = "/data/projects"
```

A `targets` list narrows the fleet to specific aliases instead of every host in your ssh config. A `[hints.<alias>]` table overrides a probed field for one host, its keys being the target's fields (`root`, `kind`, `gpu_mem_mb`, `account`, `queue`, and so on).

## ssh targets

fleet's targets are the concrete `Host` aliases in `~/.ssh/config`. Pattern entries like `Host *` are ignored, leaving the real, connectable destinations.

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

Because targets are ssh aliases, everything you already configure for ssh just works, including `ProxyJump`, identity files, and connection multiplexing. fleet reuses one connection per command. There is no agent to install on the host and no control daemon to run, only ssh and, on each host, [chefe](https://phvv.me/chefe) so the executor can build and enter the env.

## .fleet/db.json

Run state lives in one [TinyDB](https://tinydb.readthedocs.io) file at `.fleet/db.json`. It holds three things, all written from your laptop.

| store | what it remembers |
|---|---|
| host facts | each onboarded host's probed scheduler, repo root, GPU, and account |
| run registry | every dispatch, with its handle, target, script, git sha, and `--fetch` path |
| command history | each `fleet` invocation, timed, with its result |

`fleet ls` reads the cached host facts without probing again. `fleet ps` and `fleet pull` read the run registry, so they work offline. `fleet history` reads the command log, and a sibling `.fleet/fleet.log` keeps a rotated text trace. Set `FLEET_NO_HISTORY=1` to skip recording. Onboarding writes a host's facts only after `chefe install` succeeds, so the registry only ever names machines that can actually run your jobs.
