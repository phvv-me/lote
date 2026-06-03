# Configuration

lote は 3 つのものを読みます。`lote.toml` は何を送るかを、`~/.ssh/config` はどこへ送るかを、`.lote/db.json` は何が起きたかを覚えています。ホストに関するそれ以外のすべてはオンボード時に自動検出されるため、ほとんどのセットアップでは `[sync]` テーブル以外に触れることはありません。

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

### ターゲットとヒント

どちらもオプションのパワーユーザー向けの上書きです。デフォルトではターゲットは `~/.ssh/config` から来て、すべての機能が探査されるため、これらを設定することはまれです。

```toml
targets = ["miyabi", "pegasus", "dgx-spark"]   # restrict to these aliases

[hints.dgx-spark]                              # override a probed field
root = "/data/projects"
```

`targets` リストは、ssh config のすべてのホストではなく特定のエイリアスに lote を絞り込みます。`[hints.<alias>]` テーブルはひとつのホストの探査済みフィールドを上書きし、そのキーはターゲットのフィールド（`root`、`kind`、`gpu_mem_mb`、`account`、`queue` など）です。

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

## .lote/db.json

実行状態は `.lote/db.json` にあるひとつの [TinyDB](https://tinydb.readthedocs.io) ファイルに存在します。これは 3 つのものを保持し、すべてあなたのノートパソコンから書き込まれます。

| ストア | 何を覚えているか |
|---|---|
| host facts | オンボードされた各ホストの探査済みスケジューラ、リポジトリルート、GPU、アカウント |
| run registry | すべてのディスパッチ。そのハンドル、ターゲット、スクリプト、git sha、`--fetch` パスとともに |
| command history | 各 `lote` 呼び出し。時刻とその結果とともに |

`lote ls` は再度探査せずにキャッシュされた host facts を読みます。`lote ps` と `lote pull` は run registry を読むため、オフラインで動きます。`lote history` はコマンドログを読み、兄弟分の `.lote/lote.log` がローテーションされたテキストトレースを保持します。`LOTE_NO_HISTORY=1` を設定すると記録をスキップします。オンボードはホストの facts を `chefe install` が成功した後にのみ書き込むため、registry には実際にジョブを実行できるマシンしか載りません。
