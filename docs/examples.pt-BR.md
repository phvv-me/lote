# Exemplos

O tour mais rápido pelo lote são alguns fluxos de trabalho reais. Cada um é o mesmo `train.sh` alcançando uma máquina diferente, com o lote se adaptando por baixo.

## Integrar um novo host

Aponte o lote para qualquer alias ssh e deixe-o descobrir o resto.

```sh
lote ls                        # the ssh-config aliases lote can target
lote setup miyabi              # probe + rsync + chefe install, then start the queue
lote discover pegasus          # onboard and print what it is, without starting a queue
```

O `setup` encontra a raiz do repositório, envia o código, constrói o ambiente com o chefe e sonda o host em um login shell para que a toolchain do cluster fique visível. Depois disso, o lote sabe que miyabi é um host slurm e pegasus é pbs, e todo comando posterior se roteia sozinho.

## Despachar e acompanhar um job

O loop central. Enviar, lançar, observar, puxar.

```sh
lote submit miyabi train.sh --fetch research/out/run-42   # prints a handle, e.g. 1837492
lote status miyabi                                        # is it running yet?
lote logs miyabi 1837492 --follow                         # tail the merged output
lote info miyabi 1837492                                  # exit code, memory used, GPU
lote pull 1837492                                         # rsync research/out/run-42 back
```

O `submit` registra o handle com o git sha com que rodou, então um resultado em disco sempre rastreia de volta ao commit exato. Como você passou `--fetch`, o `pull` já conhece o caminho e o traz para casa por rsync sem redigitar nada.

## Rotear um job por memória

Deixe o lote escolher a máquina para um job que precisa de uma quantidade conhecida de memória.

```sh
lote submit auto train.sh --needs 40      # smallest host with >= 40 GB free, ships and launches
```

O lote considera todo host integrado, descarta os que não comportam 40 GB e escolhe o menor dos restantes, para que um job pequeno não ocupe uma máquina grande. Se nada comportar, ele te diz quanto cada host tem.

## Iterar ao vivo em um host remoto

Mantenha um host em sincronia com seu editor enquanto você depura.

```sh
lote watch dgx-spark           # re-rsync on every save, ctrl-c to stop
# in another shell:
lote submit dgx-spark smoke.sh    # the host already has your latest code
```

O `watch` respeita a mesma allowlist `[sync]` e o mesmo gitignore que o `submit`, então só os arquivos que um compute node precisa viajam, e só quando mudam. Em uma máquina comum como dgx-spark o job vai para o `pueue`, o mesmo `smoke.sh` que iria para o `sbatch` no miyabi.

## Acompanhar todos os hosts

Veja tudo de uma vez, do seu laptop, offline.

```sh
lote ps                        # the last runs across every machine, from the state store
lote reconcile miyabi          # recorded runs vs the live scheduler, with a verdict each
lote history                   # the recent lote commands you ran
```

O `ps` e o `history` leem o `.lote/db.json`, então respondem instantaneamente sem tocar em um host. O `reconcile` abre uma única conexão ssh, pergunta ao scheduler sobre cada execução registrada e rotula cada uma como ok, failed, running ou vanished, que é como você percebe um job que morreu sem um e-mail.

## Rodar o executor à mão

Em um login node você pode dirigir o executor diretamente, o mesmo código que o lote chama via ssh.

```sh
lote exec sbatch train.sh --gpus 4         # submit here, return the job id
lote exec status                           # my live jobs, squeue or qstat as fits
lote exec logs train --follow              # tail by job name, not just id
lote exec cancel all                       # stop every job of mine
```

Isso é útil quando você já está no cluster, ou ao depurar o que o lote rodaria remotamente. As diretivas em `train.sh` definem os defaults, e as flags os sobrescrevem.
