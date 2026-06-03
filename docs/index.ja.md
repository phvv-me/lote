# lote

あなたのマシンのための、ひとつのコマンドプレーン。

ssh 越しの DGX、slurm クラスタ、pbs スーパーコンピュータ、机の下の予備のマシン。それぞれが独自の方法でジョブを実行するため、同じ実験でもここでは `pueue`、あそこでは `sbatch`、別のところでは `qsub` が必要になります。**lote** は、それらすべての上に立つプレーンです。マシンを選ぶと、リポジトリをそこへ rsync し、そのホストが持つスケジューラのもとでジョブを実行し、結果を引き戻します。ひとつのジョブスクリプトがどこでも動きます。

<div class="grid cards" markdown>

- :material-rocket-launch-outline: **ひとつの submit**

    `lote submit <host> job.sh` はリポジトリを送り、ジョブを起動し、ハンドルを表示します。スケジューラが何であっても変わりません。

- :material-transit-connection-variant: **どんなスケジューラでも**

    lote は各ホストのキューを検出して適応します。素のマシンでは `pueue`、slurm では `sbatch`、pbs では `qsub`。

- :material-radar: **ホストをまたいだ状態管理**

    `lote ps` と `lote reconcile` は、ノートパソコンからすべてのマシンの実行を追跡します。

- :material-lan-connect: **ssh だけ**

    ターゲットはあなたの `~/.ssh/config` のホストエイリアスです。インストールするエージェントも、面倒を見る制御デーモンもありません。

</div>

## インストール

```sh
pip install lote            # the lote command, ready to use
chefe add lote --pypi       # or pull it into a chefe project
```

lote はオンホストエグゼキュータを ssh 越しに `chefe run lote exec ...` としてディスパッチするため、各ホストには [chefe](https://phvv.me/chefe)（兄弟分の環境ツール）が必要です。これは `lote setup` がインストールし、初回実行時に pixi を立ち上げます。

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

!!! warning "lote はまだ初期段階です（`0.0.x`）"
    設定フォーマットとコマンドは今後変更される可能性があります。

## 次へ

<div class="grid cards" markdown>

- [:material-cogs: **How it works**](how-it-works.md) — ディスパッチパイプラインとその 3 つの層。
- [:material-console: **Commands**](commands.md) — CLI の全体。
- [:material-tune: **Configuration**](configuration.md) — `lote.toml`、ssh ターゲット、状態ストア。
- [:material-test-tube: **Examples**](examples.md) — 実際のエンドツーエンドのワークフロー。

</div>
