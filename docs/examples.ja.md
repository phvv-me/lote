# Examples

lote を最速で見て回るには、いくつかの実際のワークフローが一番です。それぞれが同じ `train.sh` が異なるマシンに届く様子であり、その下で lote が適応します。

## 新しいホストをオンボードする

任意の ssh エイリアスに lote を向け、残りを発見させます。

```sh
lote ls                        # the ssh-config aliases lote can target
lote setup miyabi              # probe + rsync + chefe install, then start the queue
lote discover pegasus          # onboard and print what it is, without starting a queue
```

`setup` はリポジトリルートを見つけ、コードを送り、chefe で環境をビルドし、クラスタのツールチェーンが見えるようにログインシェルでホストを探査します。この後、lote は miyabi が slurm ホストであり pegasus が pbs であることを知り、以降のすべてのコマンドが自分自身をルーティングします。

## ジョブをディスパッチして追う

中心となるループです。送り、起動し、見守り、引き戻します。

```sh
lote submit miyabi train.sh --fetch research/out/run-42   # prints a handle, e.g. 1837492
lote status miyabi                                        # is it running yet?
lote logs miyabi 1837492 --follow                         # tail the merged output
lote info miyabi 1837492                                  # exit code, memory used, GPU
lote pull 1837492                                         # rsync research/out/run-42 back
```

`submit` はハンドルを、それが実行された git sha とともに記録するため、ディスク上の結果は常に正確なコミットまでたどれます。`--fetch` を渡したので、`pull` はすでにパスを知っており、再入力なしでそれを引き戻します。

## メモリでジョブをルーティングする

既知の量のメモリを必要とするジョブのために、lote にマシンを選ばせます。

```sh
lote submit auto train.sh --needs 40      # smallest host with >= 40 GB free, ships and launches
```

lote はオンボードされたすべてのホストを検討し、40 GB に収まらないものを除外し、残りの中で最小のものを選びます。これにより、小さなジョブが大きなマシンを占有しません。何も収まらない場合は、各ホストが何を持っているかを教えてくれます。

## リモートホスト上でライブにイテレートする

デバッグ中に、エディタとホストを歩調を合わせ続けます。

```sh
lote watch dgx-spark           # re-rsync on every save, ctrl-c to stop
# in another shell:
lote submit dgx-spark smoke.sh    # the host already has your latest code
```

`watch` は同じ `[sync]` 許可リストと gitignore を `submit` と同様に尊重するため、計算ノードが必要とするファイルだけが、変更されたときにだけ送られます。dgx-spark のような素のマシンではジョブは `pueue` に行き、miyabi では `sbatch` に当たるのと同じ `smoke.sh` です。

## すべてのホストを追跡する

すべてを一度に、ノートパソコンから、オフラインで見ます。

```sh
lote ps                        # the last runs across every machine, from the state store
lote reconcile miyabi          # recorded runs vs the live scheduler, with a verdict each
lote history                   # the recent lote commands you ran
```

`ps` と `history` は `.lote/db.sqlite` を読むため、ホストに触れずに即座に答えます。`reconcile` はひとつの ssh 接続を開き、各記録済み実行についてスケジューラに尋ね、それぞれを ok、failed、running、vanished とラベル付けします。これは、メールなしで死んだジョブに気づく方法です。

## エグゼキュータを手で実行する

ログインノードでは、lote が ssh 越しに呼び出すのと同じコードを、エグゼキュータを直接駆動できます。

```sh
lote exec sbatch train.sh --gpus 4         # submit here, return the job id
lote exec status                           # my live jobs, squeue or qstat as fits
lote exec logs train --follow              # tail by job name, not just id
lote exec cancel all                       # stop every job of mine
```

これは、すでにクラスタ上にいるときや、lote がリモートで何を実行するかをデバッグするときに役立ちます。`train.sh` 内のディレクティブがデフォルトを設定し、フラグがそれらを上書きします。
