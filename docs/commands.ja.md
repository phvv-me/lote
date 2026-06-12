# Commands

lote には 2 つのコマンドグループがあります。トップレベルの `lote` 動詞はあなたのノートパソコンで実行するコントロールプレーンであり、`lote exec` グループはひとつのマシンで動くオンホストエグゼキュータです。ターゲットは全体を通して `~/.ssh/config` のホストエイリアスです。

## コントロールプレーン

| コマンド | 何をするか |
|---|---|
| `lote ls` | キャッシュされた機能とともに ssh-config のターゲットを一覧表示（探査はしない） |
| `lote discover <target>` | ホストをオンボードしてノードクラスを把握（probe + sync + `chefe install` + キューごとの探査ジョブ） |
| `lote setup <target>` | ホストをオンボードし、そのキューデーモンを起動 |
| `lote submit <target\|auto> <script> [args…]` | リポジトリを rsync で上げて `script` を起動、ハンドルを表示 |
| `lote ps [limit]` | すべてのターゲットにまたがる最近のディスパッチ済み実行 |
| `lote status <target>` | ターゲット上のライブなジョブ |
| `lote reconcile <target>` | ターゲットの記録済み実行をライブなスケジューラと比較 |
| `lote interact <target>` | インタラクティブセッション（本物の TTY）を取得 |
| `lote logs <target> <handle>` | 実行のログを tail |
| `lote info <target> <handle>` | ジョブの事後検証（終了コード、メモリ、GPU） |
| `lote fetch <target> <path>` | 結果のパスを rsync で引き戻す |
| `lote pull <handle>` | submit 時に記録された結果のパスを rsync で引き戻す |
| `lote watch <target>` | ローカルファイルが変更されるたびにリポジトリを再同期 |
| `lote history [limit]` | 最近の `lote` コマンドの実行履歴 |

## setup と discover

```sh
lote discover miyabi          # onboard, then print the host's kind, root, and node classes
lote setup miyabi             # same onboarding, and start the pueue daemon
lote discover miyabi --wait 120   # give each queue's probe job at most two minutes
```

オンボードはホストのリポジトリルート（あれば HPC の `/work` エリア、なければ `~/projects`）を見つけ、そこへリポジトリを rsync し、`chefe install` を実行し、それからログインシェルでホストのスケジューラ、GPU、アカウントを探査します。ホストは `chefe install` が成功して初めてキャッシュされるため、環境をビルドできないものは決してターゲットになりません。

スケジューラのあるホストでは、ディスカバリは続けてノードクラスを把握します。スケジューラにキュー（PBS では `qstat -q`、SLURM では `sinfo`）を尋ね、キューごとに [mainboard](https://phvv.me/mainboard) のマシンスナップショットを出力する最小ジョブをひとつ投入するので、各キューの実際の GPU、メモリ、コアが Miyabi の `prepost` ムーバーのような特別なクラスも含めてそのクラスキーの下にキャッシュされます。ジョブを拒否したり `--wait` を超えて混み続けるキューは警告とともにスキップされ、次の discover が拾います。

## submit

```sh
lote submit miyabi train.sh                       # to a named host
lote submit miyabi train.sh --fetch results/run1  # remember a results path for `pull`
lote submit auto train.sh --needs 40              # route by free memory, pick smallest fit
```

`submit` はリポジトリを rsync し、ターゲットのスケジューラを選んでスクリプトを起動し、ハンドルとともに git sha（と dirty フラグ）を記録します。`auto` の場合は `--needs <GB>` が必須で、lote はなお収まる最小メモリのホストを選び、大きなマシンを空けておきます。スクリプトの後ろの追加の位置引数は `ARGS` 環境変数を通じてジョブに届きます。

## ps、status、reconcile

```sh
lote ps                       # the last runs across every machine
lote status miyabi            # what is live on one host right now
lote reconcile miyabi         # recorded runs vs the live scheduler
```

`ps` はローカルの状態ストアを読むため、オフラインでも動きます。`status` は ssh 接続を開き、ホストのスケジューラに直接尋ねます。`reconcile` はこの 2 つを結合し、各記録済み実行にライブな状態、終了コード、ひとことの判定（ok、failed、running、vanished）を与えます。これは、キューを手で更新し続ける作業を置き換える、ローカル状態のデバッグ補助です。

## logs、info、fetch、pull

```sh
lote logs miyabi <handle> --follow    # tail the merged stdout+stderr
lote info miyabi <handle>             # exit status, memory used vs cap, GPU usage
lote fetch miyabi research/out        # pull an arbitrary path back
lote pull <handle>                    # pull the path recorded at submit time
```

`fetch` はリポジトリルートからの相対パスを取り、同じローカルパスへ rsync で引き戻します。`pull` は submit 時に `--fetch` を渡した場合のショートカットで、パスを再入力する必要がありません。

## interact と watch

```sh
lote interact miyabi --gpus 2 --hours 4   # interactive session (qsub -I on pbs, ssh -t otherwise)
lote interact miyabi --dry-run            # print the command instead of running it
lote watch miyabi                         # re-rsync on every local file change, ctrl-c to stop
```

pbs ホストでは、`interact` は検出されたアカウントとキューでインタラクティブな `qsub -I` を submit します。それ以外のホストではログインシェルを開きます。`watch` は同じ許可リストと gitignore を `submit` と同様に尊重しながら、あなたの作業ツリーとホストを同期させ続けます。

## exec

オンホストエグゼキュータです。lote が ssh 越しに呼び出しますが、ログインノードで直接実行することもできます。

| コマンド | 何をするか |
|---|---|
| `lote exec qsub <script> [args…]` | pbs に submit し、ジョブ id を返す |
| `lote exec sbatch <script> [args…]` | slurm に submit し、ジョブ id を返す |
| `lote exec run <script> [args…]` | bash で実行、スケジューラなし |
| `lote exec status` | 自分のライブなジョブのテーブル |
| `lote exec info <jid>` | ジョブの事後検証レコード |
| `lote exec logs <jid\|name> [--follow]` | 最新の一致するログを tail |
| `lote exec cancel <jid\|name\|all>` | `qdel` または `scancel` |

```sh
lote exec qsub train.sh --select 2 --walltime 04:00:00
lote exec sbatch train.sh --gpus 4 --partition gpu
lote exec run smoke.sh                 # plain bash, off a cluster
lote exec status                       # squeue --me on slurm, otherwise qstat
lote exec cancel all                   # every job of mine
```

`qsub` と `sbatch` はスクリプトの `#PBS` または `#SBATCH` ディレクティブを読み、フラグでそれらを上書きできるため、ひとつのスクリプトが妥当なデフォルトを持ち運びます。スクリプト名だけを指定すると実験の `jobs/` ディレクトリに対して解決され、`status`、`logs`、`info` はホストのスケジューラを自動的に選びます。
