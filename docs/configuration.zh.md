# 配置

lote 读取三样东西。`lote.toml` 说明要发送什么，`~/.ssh/config` 说明发送到哪里，`.lote/db.json` 记住发生了什么。关于一台主机的其他一切都会在你接入它时自动发现，因此大多数配置除了 `[sync]` 表之外都无需改动。

## lote.toml

位于仓库根目录的 `lote.toml` 本质上就是 rsync 的范围。`include` 是计算节点所需路径的允许列表，而 `exclude` 会跳过其中那些笨重或可重新生成的模式。

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

`include` 路径会通过 `rsync -R` 保留，因此目录树会落地到主机上的同一位置。`exclude` 模式叠加在你的 gitignore 之上（lote 已经遵循 gitignore），因此你只需在这里列出额外的笨重产物。`lote watch` 监视同一个 `include` 集合，并在其中任何未被忽略的文件发生变化时重新同步。

### 目标和提示

两者都是可选的高级用户覆盖项。默认情况下，目标来自 `~/.ssh/config`，并且每一项能力都会被探测，因此你很少需要设置它们。

```toml
targets = ["miyabi", "pegasus", "dgx-spark"]   # restrict to these aliases

[hints.dgx-spark]                              # override a probed field
root = "/data/projects"
```

`targets` 列表将 lote 缩小到特定的别名，而不是你 ssh 配置中的每一台主机。`[hints.<alias>]` 表为某一台主机覆盖一个被探测的字段，其键是该目标的字段（`root`、`kind`、`gpu_mem_mb`、`account`、`queue` 等等）。

## ssh 目标

lote 的目标是 `~/.ssh/config` 中具体的 `Host` 别名。像 `Host *` 这样的模式条目会被忽略，只留下真实的、可连接的目的地。

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

由于目标就是 ssh 别名，你为 ssh 配置的一切都能直接生效，包括 `ProxyJump`、身份文件和连接复用。lote 每个命令复用一条连接。主机上没有需要安装的 agent，也没有需要运行的控制守护进程，只有 ssh，以及每台主机上的 [chefe](https://phvv.me/chefe)，让执行器能够构建并进入环境。

## .lote/db.json

运行状态保存在 `.lote/db.json` 这一个 [TinyDB](https://tinydb.readthedocs.io) 文件中。它保存三样东西，全部从你的笔记本上写入。

| 存储 | 它记住了什么 |
|---|---|
| 主机信息 | 每台已接入主机被探测到的调度器、仓库根目录、GPU 和账户 |
| 运行注册表 | 每一次分发，连同它的句柄、目标、脚本、git sha 和 `--fetch` 路径 |
| 命令历史 | 每一次 `lote` 调用，带时间，连同其结果 |

`lote ls` 读取缓存的主机信息而不再次探测。`lote ps` 和 `lote pull` 读取运行注册表，因此可以离线工作。`lote history` 读取命令日志，配套的 `.lote/lote.log` 保留一份轮转的文本追踪。设置 `LOTE_NO_HISTORY=1` 可跳过记录。接入过程只在 `chefe install` 成功之后才写入主机信息，因此注册表里只会出现确实能运行你作业的机器。
