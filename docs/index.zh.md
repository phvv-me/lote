# lote

为你的所有机器提供一个统一的命令平面。

一台通过 ssh 连接的 DGX、一个 slurm 集群、一台 pbs 超级计算机、桌下那台备用机器。每一台都以自己的方式运行作业，所以同一个实验在这里需要 `pueue`，在那里需要 `sbatch`，在别处又需要 `qsub`。**lote** 就是凌驾于它们之上的那个平面。你选定一台机器，它便把你的仓库 rsync 过去，在那台主机所拥有的任意调度器下运行你的作业，再把结果拉回来。一个作业脚本，到哪儿都能跑。

<div class="grid cards" markdown>

- :material-rocket-launch-outline: **一次提交**

    `lote submit <host> job.sh` 发送仓库、启动作业并打印一个句柄。无论是哪种调度器。

- :material-transit-connection-variant: **任意调度器**

    lote 检测每台主机的队列并自适应。普通机器上用 `pueue`，slurm 上用 `sbatch`，pbs 上用 `qsub`。

- :material-radar: **跨主机的状态**

    `lote ps` 和 `lote reconcile` 从你的笔记本上跟踪每台机器上的每一次运行。

- :material-lan-connect: **只需 ssh**

    目标就是你 `~/.ssh/config` 中的主机别名。无需安装 agent，无需照看控制守护进程。

</div>

## 安装

```sh
pip install lote            # the lote command, ready to use
chefe add lote --pypi       # or pull it into a chefe project
```

lote 通过 ssh 以 `chefe run lote exec ...` 的形式分发它的主机端执行器，因此每台主机都需要 [chefe](https://phvv.me/chefe)（配套的环境工具），`lote setup` 会安装它，并在首次运行时启动 pixi。

```toml title="lote.toml"
[sync]
include = ["research", "toolbox", "pixi.toml"]   # what a compute node needs
exclude = ["**/.venv", "**/__pycache__", "**/*.ckpt"]   # heavy, regenerable
```

```sh
lote setup miyabi                 # onboard a host: probe, sync, build the env
lote submit miyabi train.sh       # ship the repo and launch, prints a handle
lote ps                           # recent runs across every machine
lote pull <handle>                # rsync the results back
```

!!! warning "lote 尚处早期（`0.0.x`）"
    配置格式和命令仍可能变化。

## 下一步

<div class="grid cards" markdown>

- [:material-cogs: **工作原理**](how-it-works.md) — 分发流水线及其三个层次。
- [:material-console: **命令**](commands.md) — 完整的 CLI。
- [:material-tune: **配置**](configuration.md) — `lote.toml`、ssh 目标和状态存储。
- [:material-test-tube: **示例**](examples.md) — 真实的端到端工作流。

</div>
