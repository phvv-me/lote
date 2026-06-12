# Configuration

lote は 3 つのものを読みます。`lote.toml` は何を送るかを、`~/.ssh/config` はどこへ送るかを、`.lote/db.sqlite` は何が起きたかを覚えています。ホストに関するそれ以外のすべてはオンボード時に自動検出されるため、ほとんどのセットアップでは `[sync]` テーブル以外に触れることはありません。

## lote.toml

リポジトリルートの `lote.toml` は、本質的には rsync のスコープにすぎません。`include` は計算ノードが必要とするパスの許可リストであり、`exclude` はその中の重いものや再生成可能なパターンをスキップします。

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

`include` のパスは `rsync -R` で保持されるため、ツリーはホスト上の同じ場所に着地します。`exclude` パターンはあなたの gitignore の上に積み重なり、lote はその gitignore を既に尊重しているため、ここには余分な重いアーティファクトだけを列挙すればよいのです。`lote watch` は同じ `include` セットを監視し、その中の無視されていないファイルが変更されると再同期します。

同期はローカルのツリーをミラーリングします。`include` セットの中でローカルに存在しなくなったものはホスト上で刈り取られる（`rsync --delete`）ため、リネームされたモジュールが古いコピーを残して新しいものを覆い隠すことはありません。ホストにしか存在しないアーティファクトは `protect` によって生き残ります。これは rsync のフィルタパターンのリストで、デフォルトでは `.lote/***`、`results/***`、`logs/***`、`*.log` を保護します（`dir/***` 形式はディレクトリとその中のすべてに、任意の深さでマッチします）。除外されたパスも削除されることはありません。`protect` を設定するとデフォルトが置き換えられるため、追加だけしたい場合はデフォルトも繰り返してください。

```toml
[sync]
protect = [".lote/***", "results/***", "logs/***", "*.log", "checkpoints/***"]
```
macOS ではアップストリームの rsync をインストールしてください（`brew install rsync`）。Apple 同梱の openrsync はリモートホスト上で刈り取りができず、lote はそれによるミラーリングを拒否します。

### ターゲットとヒント

どちらもオプションのパワーユーザー向けの上書きです。デフォルトではターゲットは `~/.ssh/config` から来て、すべての機能が探査されるため、これらを設定することはまれです。

```toml
targets = ["miyabi", "pegasus", "dgx-spark"]   # restrict to these aliases

[hints.dgx-spark]                              # override a probed field
root = "/data/projects"
```

`targets` リストは、ssh config のすべてのホストではなく特定のエイリアスに lote を絞り込みます。`[hints.<alias>]` テーブルはひとつのホストの探査済みフィールドを上書きし、そのキーはターゲットのアイデンティティフィールド（`root`、`kind`、`account`、`queue`、`login_shell`）です。ハードウェア能力はノードクラスごとに探査されるためヒントでは指定できません。詳しくは次のセクションで説明します。

## ssh ターゲット

lote のターゲットは `~/.ssh/config` 内の具体的な `Host` エイリアスです。`Host *` のようなパターンエントリは無視され、実際に接続可能な宛先が残ります。

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

ターゲットは ssh エイリアスなので、`ProxyJump`、identity ファイル、接続の多重化など、ssh ですでに設定しているものすべてがそのまま動きます。lote はコマンドごとにひとつの接続を再利用します。ホストにインストールするエージェントも、実行する制御デーモンもなく、必要なのは ssh と、各ホスト上の [chefe](https://phvv.me/chefe)（エグゼキュータが環境をビルドして入れるようにするため）だけです。

## ノードクラス

ホストが 1 台のマシンであることはまれです。ssh のログインノードはひとつのノードクラスであり、スケジューラの各キュー（PBS のキュー、SLURM のパーティション）はそれぞれ固有のハードウェアを抱えています。HPC ホストの GPU キューはログインノードとはまったく別物で、Miyabi の `prepost` データムーバーのような特別なキューもまた違います。そのため lote は能力をノードクラスごとに保持します。

`login` クラスは ssh 越しの標準探査から来ます。ホストに何もインストールする必要はありません。それ以外のクラスはオンボード中に発見されます。スケジューラにキュー一覧（PBS では `qstat -q`、SLURM では `sinfo`）を尋ね、各キューに [mainboard](https://phvv.me/mainboard) のマシンスナップショットを出力するだけの最小ジョブをひとつ投入します。解析されたスナップショットがそのキューのクラスとなり、探査ジョブが返ってくるたびにクラスキーの下へキャッシュされ、`lote ls` は各クラスをホストの下に一覧します。ジョブを拒否したり、失敗させたり、`--wait`（デフォルト 600 秒）を超えて混み続けるキューは警告とともにスキップされ、discover 自体が失敗することはありません。ssh ホストにはキューがないため、そのオンボードは従来どおりログインノードの単一探査のままです。

## .lote/db.sqlite

実行状態は `.lote/db.sqlite` にあるひとつの WAL モード [SQLite](https://www.sqlite.org) ファイルに存在します。SQLite はファイル全体の書き換えなしに並行安全な upsert を提供するため、並列の `lote` 呼び出しがストアを壊すことはありません。これは 4 つのものを保持し、すべてあなたのノートパソコンから書き込まれます。

| ストア | 何を覚えているか |
|---|---|
| host facts | オンボードされた各ホストの探査済みスケジューラ、リポジトリルート、アカウント |
| node classes | `(ホスト, クラス)` キーごとに 1 行。そのクラスの探査済み GPU、メモリ、コア |
| run registry | すべてのディスパッチ。そのハンドル、ターゲット、スクリプト、git sha、`--fetch` パスとともに |
| command history | 各 `lote` 呼び出し。時刻とその結果とともに |

`lote ls` は再度探査せずにキャッシュされた facts とクラスを読みます。`lote ps` と `lote pull` は run registry を読むため、オフラインで動きます。`lote history` はコマンドログを読み、兄弟分の `.lote/lote.log` がローテーションされたテキストトレースを保持します。`LOTE_NO_HISTORY=1` を設定すると記録をスキップします。オンボードはホストの facts を `chefe install` が成功した後にのみ書き込むため、registry には実際にジョブを実行できるマシンしか載りません。
