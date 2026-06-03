# 示例

了解 lote 最快的方式就是几个真实的工作流。每一个都是同一个 `train.sh` 到达不同的机器，而 lote 在底层自适应。

## 接入一台新主机

把 lote 指向任意一个 ssh 别名，让它来发现其余的一切。

```sh
lote ls                        # the ssh-config aliases lote can target
lote setup miyabi              # probe + rsync + chefe install, then start the queue
lote discover pegasus          # onboard and print what it is, without starting a queue
```

`setup` 找到仓库根目录，发送代码，用 chefe 构建环境，并在登录 shell 中探测主机以便集群工具链可见。在此之后，lote 就知道 miyabi 是一台 slurm 主机、pegasus 是 pbs，后续每一个命令都会自行路由。

## 分发并跟踪一个作业

核心循环。发送、启动、观察、拉取。

```sh
lote submit miyabi train.sh --fetch research/out/run-42   # prints a handle, e.g. 1837492
lote status miyabi                                        # is it running yet?
lote logs miyabi 1837492 --follow                         # tail the merged output
lote info miyabi 1837492                                  # exit code, memory used, GPU
lote pull 1837492                                         # rsync research/out/run-42 back
```

`submit` 把句柄连同它运行所用的 git sha 一起记录下来，因此磁盘上的每一份结果总能追溯到确切的提交。由于你传入了 `--fetch`，`pull` 已经知道路径，会把它 rsync 回家而无需重新输入。

## 按内存路由一个作业

让 lote 为一个需要已知内存量的作业挑选机器。

```sh
lote submit auto train.sh --needs 40      # smallest host with >= 40 GB free, ships and launches
```

lote 会考虑每一台已接入的主机，剔除无法容纳 40 GB 的那些，并在其余主机中挑选最小的一台，这样一个小作业就不会占用一台大机器。如果没有任何主机合适，它会告诉你每台主机各有多少。

## 在远程主机上实时迭代

在你调试时让一台主机与你的编辑器保持同步。

```sh
lote watch dgx-spark           # re-rsync on every save, ctrl-c to stop
# in another shell:
lote submit dgx-spark smoke.sh    # the host already has your latest code
```

`watch` 遵循与 `submit` 相同的 `[sync]` 允许列表和 gitignore，因此只有计算节点所需的文件才会传输，并且只在它们变化时传输。在像 dgx-spark 这样的普通机器上，作业会进入 `pueue`，正是那个在 miyabi 上会命中 `sbatch` 的同一个 `smoke.sh`。

## 跟踪每一台主机

从你的笔记本上，离线地，一次看到全部。

```sh
lote ps                        # the last runs across every machine, from the state store
lote reconcile miyabi          # recorded runs vs the live scheduler, with a verdict each
lote history                   # the recent lote commands you ran
```

`ps` 和 `history` 读取 `.lote/db.json`，因此无需触碰任何主机即可即时给出答案。`reconcile` 打开一条 ssh 连接，就每一次已记录的运行询问调度器，并把每一次标记为 ok、failed、running 或 vanished，这正是你发现某个作业悄无声息地死掉的方式。

## 手动运行执行器

在登录节点上，你可以直接驱动执行器，也就是 lote 通过 ssh 调用的同一份代码。

```sh
lote exec sbatch train.sh --gpus 4         # submit here, return the job id
lote exec status                           # my live jobs, squeue or qstat as fits
lote exec logs train --follow              # tail by job name, not just id
lote exec cancel all                       # stop every job of mine
```

当你已经在集群上时，或者在调试 lote 会远程运行什么时，这很有用。`train.sh` 中的指令设定默认值，而标志会覆盖它们。
