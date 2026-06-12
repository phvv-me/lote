# 配置

lote 读取三样东西。`lote.toml` 说明要发送什么，`~/.ssh/config` 说明发送到哪里，`.lote/db.sqlite` 记住发生了什么。关于一台主机的其他一切都会在你接入它时自动发现，因此大多数配置除了 `[sync]` 表之外都无需改动。

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

同步会镜像本地目录树。`include` 集合内任何在本地已不存在的内容都会在主机上被清除（`rsync --delete`），因此重命名的模块绝不会留下过时副本来遮蔽它的替代者。只存在于主机上的产物通过 `protect` 得以幸存，它是一个 rsync 过滤模式列表，默认保护 `.lote/***`、`results/***`、`logs/***` 和 `*.log`（`dir/***` 形式匹配一个目录及其内部的所有内容，不限深度）。被排除的路径也永远不会被删除。设置 `protect` 会替换默认值，所以如果你只是想追加，请把默认值也重复写上：

```toml
[sync]
protect = [".lote/***", "results/***", "logs/***", "*.log", "checkpoints/***"]
```
在 macOS 上请安装上游 rsync（`brew install rsync`），因为 Apple 自带的 openrsync 无法在远程主机上执行清除，lote 会拒绝用它进行镜像。

### 目标和提示

两者都是可选的高级用户覆盖项。默认情况下，目标来自 `~/.ssh/config`，并且每一项能力都会被探测，因此你很少需要设置它们。

```toml
targets = ["miyabi", "pegasus", "dgx-spark"]   # restrict to these aliases

[hints.dgx-spark]                              # override a probed field
root = "/data/projects"
```

`targets` 列表将 lote 缩小到特定的别名，而不是你 ssh 配置中的每一台主机。`[hints.<alias>]` 表为某一台主机覆盖一个被探测的字段，其键是该目标的身份字段（`root`、`kind`、`account`、`queue`、`login_shell`）。硬件能力无法通过 hint 指定，因为它们是按节点类别探测的，下一节会解释。

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

## 节点类别

一台主机很少只是一台机器。ssh 登录节点是一类节点，而调度器的每个队列（PBS 队列、SLURM 分区）背后都是自己的硬件，所以 HPC 主机的 GPU 队列和它的登录节点完全不同，像 Miyabi 的 `prepost` 数据搬运队列这样的特殊队列又是另一回事。因此 lote 按节点类别保存能力信息。

`login` 类别来自标准的 ssh 探测，主机上无需安装任何东西。其余类别在接入时被发现。lote 向调度器询问队列列表（PBS 上是 `qstat -q`，SLURM 上是 `sinfo`），并向每个队列提交一个最小作业，其全部内容就是打印 [mainboard](https://phvv.me/mainboard) 的机器快照。解析后的快照成为该队列的类别，随着探测作业陆续返回而缓存在各自的类别键下，`lote ls` 会在主机下方列出每个类别。拒绝作业、作业失败或排队超过 `--wait`（默认 600 秒）的队列会被跳过并给出警告，绝不会让 discover 失败。ssh 主机没有队列，所以它的接入仍然是一直以来那一次登录节点探测。

## .lote/db.sqlite

运行状态保存在 `.lote/db.sqlite` 这一个 WAL 模式的 [SQLite](https://www.sqlite.org) 文件中。SQLite 提供并发安全的 upsert 且无需重写整个文件，因此并行的 `lote` 调用永远不会损坏存储。它保存四样东西，全部从你的笔记本上写入。

| 存储 | 它记住了什么 |
|---|---|
| 主机信息 | 每台已接入主机被探测到的调度器、仓库根目录和账户 |
| 节点类别 | 每个 `(主机, 类别)` 键一行，记录该类别被探测到的 GPU、内存和核心数 |
| 运行注册表 | 每一次分发，连同它的句柄、目标、脚本、git sha 和 `--fetch` 路径 |
| 命令历史 | 每一次 `lote` 调用，带时间，连同其结果 |

`lote ls` 读取缓存的主机信息和类别而不再次探测。`lote ps` 和 `lote pull` 读取运行注册表，因此可以离线工作。`lote history` 读取命令日志，配套的 `.lote/lote.log` 保留一份轮转的文本追踪。设置 `LOTE_NO_HISTORY=1` 可跳过记录。接入过程只在 `chefe install` 成功之后才写入主机信息，因此注册表里只会出现确实能运行你作业的机器。
