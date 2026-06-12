# 命令

lote 有两个命令组。顶层的 `lote` 动词是你在笔记本上运行的控制平面，而 `lote exec` 组是运行在单台机器上的主机端执行器。全程的目标都是 `~/.ssh/config` 中的主机别名。

## 控制平面

| 命令 | 作用 |
|---|---|
| `lote ls` | 列出 ssh-config 目标及其缓存的能力（从不探测） |
| `lote discover <target>` | 接入一台主机并测绘其节点类别（探测 + 同步 + `chefe install` + 每个队列一个探测作业） |
| `lote setup <target>` | 接入一台主机并启动它的队列守护进程 |
| `lote submit <target\|auto> <script> [args…]` | 将仓库 rsync 上传并启动 `script`，打印一个句柄 |
| `lote ps [limit]` | 跨每个目标的最近已分发运行 |
| `lote status <target>` | 某个目标上的实时作业 |
| `lote reconcile <target>` | 将某个目标的已记录运行与实时调度器进行比较 |
| `lote interact <target>` | 获取一个交互式会话（真实的 TTY） |
| `lote logs <target> <handle>` | tail 某次运行的日志 |
| `lote info <target> <handle>` | 某个作业的事后分析（退出码、内存、GPU） |
| `lote fetch <target> <path>` | 将一个结果路径 rsync 回来 |
| `lote pull <handle>` | rsync 回提交时记录的结果路径 |
| `lote watch <target>` | 在每次本地文件变更时重新同步仓库 |
| `lote history [limit]` | 最近的 `lote` 命令调用 |

## setup 和 discover

```sh
lote discover miyabi          # onboard, then print the host's kind, root, and node classes
lote setup miyabi             # same onboarding, and start the pueue daemon
lote discover miyabi --wait 120   # give each queue's probe job at most two minutes
```

接入过程会找到主机的仓库根目录（若存在 HPC 的 `/work` 区域则用它，否则用 `~/projects`），把仓库 rsync 过去，运行 `chefe install`，然后在登录 shell 中探测主机的调度器、GPU 和账户。一台主机只有在 `chefe install` 成功之后才会被缓存，因此无法构建环境的主机永远不会成为目标。

在有调度器的主机上，发现过程接着会测绘节点类别。它向调度器询问队列（PBS 上是 `qstat -q`，SLURM 上是 `sinfo`），并为每个队列提交一个打印 [mainboard](https://phvv.me/mainboard) 机器快照的最小作业，这样每个队列真实的 GPU、内存和核心数就会缓存在对应的类别键下，包括像 Miyabi 的 `prepost` 搬运队列这样的特殊类别。拒绝作业或排队超过 `--wait` 的队列会被跳过并给出警告，由下一次 discover 补上。

## submit

```sh
lote submit miyabi train.sh                       # to a named host
lote submit miyabi train.sh --fetch results/run1  # remember a results path for `pull`
lote submit auto train.sh --needs 40              # route by free memory, pick smallest fit
```

`submit` 将仓库 rsync 上传，挑选目标的调度器，并启动脚本，同时把 git sha（以及一个 dirty 标志）连同句柄一起记录下来。使用 `auto` 时必须提供 `--needs <GB>`，lote 会选择仍能容纳作业的最小内存主机，从而让大机器保持空闲。脚本之后的额外位置参数会通过 `ARGS` 环境变量传给作业。

## ps、status 和 reconcile

```sh
lote ps                       # the last runs across every machine
lote status miyabi            # what is live on one host right now
lote reconcile miyabi         # recorded runs vs the live scheduler
```

`ps` 读取本地状态存储，因此可以离线工作。`status` 打开一条 ssh 连接，直接向主机的调度器询问。`reconcile` 把两者结合起来，为每一次已记录的运行给出实时状态、退出码和一个单词的判定（ok、failed、running 或 vanished）。这是替代手动刷新队列的本地状态调试辅助手段。

## logs、info、fetch 和 pull

```sh
lote logs miyabi <handle> --follow    # tail the merged stdout+stderr
lote info miyabi <handle>             # exit status, memory used vs cap, GPU usage
lote fetch miyabi research/out        # pull an arbitrary path back
lote pull <handle>                    # pull the path recorded at submit time
```

`fetch` 接受一个相对于仓库根目录的路径，并将其 rsync 回相同的本地路径。`pull` 是你在提交时传入了 `--fetch` 之后的快捷方式，这样你就无需再次输入路径。

## interact 和 watch

```sh
lote interact miyabi --gpus 2 --hours 4   # interactive session (qsub -I on pbs, ssh -t otherwise)
lote interact miyabi --dry-run            # print the command instead of running it
lote watch miyabi                         # re-rsync on every local file change, ctrl-c to stop
```

在 pbs 主机上，`interact` 用发现到的账户和队列提交一个交互式的 `qsub -I`。在其他任何主机上，它会打开一个登录 shell。`watch` 在你迭代时让一台主机与你的工作树保持同步，遵循与 `submit` 相同的允许列表和 gitignore。

## exec

主机端执行器。lote 通过 ssh 调用它，但你也可以在登录节点上直接运行它。

| 命令 | 作用 |
|---|---|
| `lote exec qsub <script> [args…]` | 提交到 pbs，返回作业 id |
| `lote exec sbatch <script> [args…]` | 提交到 slurm，返回作业 id |
| `lote exec run <script> [args…]` | 通过 bash 运行，无调度器 |
| `lote exec status` | 我的实时作业表格 |
| `lote exec info <jid>` | 某个作业的事后分析记录 |
| `lote exec logs <jid\|name> [--follow]` | tail 最近的匹配日志 |
| `lote exec cancel <jid\|name\|all>` | `qdel` 或 `scancel` |

```sh
lote exec qsub train.sh --select 2 --walltime 04:00:00
lote exec sbatch train.sh --gpus 4 --partition gpu
lote exec run smoke.sh                 # plain bash, off a cluster
lote exec status                       # squeue --me on slurm, otherwise qstat
lote exec cancel all                   # every job of mine
```

`qsub` 和 `sbatch` 会读取脚本中的 `#PBS` 或 `#SBATCH` 指令，并允许标志覆盖它们，因此一个脚本就能携带合理的默认值。裸脚本名称会在实验的 `jobs/` 目录中解析，而 `status`、`logs` 和 `info` 会自动挑选主机的调度器。
