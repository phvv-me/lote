# Comandos

O lote tem dois grupos de comandos. Os verbos `lote` de nível superior são o plano de controle que você roda no seu laptop, e o grupo `lote exec` é o executor on-host que roda em uma única máquina. Os alvos são aliases de host do `~/.ssh/config` por toda parte.

## Plano de controle

| comando | o que faz |
|---|---|
| `lote ls` | lista os alvos do ssh-config com suas capacidades em cache (nunca faz probe) |
| `lote discover <target>` | integra um host e mapeia suas classes de nó (probe + sync + `chefe install` + um job de sondagem por fila) |
| `lote setup <target>` | integra um host e inicia seu daemon de fila |
| `lote submit <target\|auto> <script> [args…]` | faz rsync do repositório para cima e lança `script`, imprime um handle |
| `lote ps [limit]` | execuções despachadas recentemente em todos os alvos |
| `lote status <target>` | jobs ativos em um alvo |
| `lote reconcile <target>` | compara as execuções registradas de um alvo com o scheduler ao vivo |
| `lote interact <target>` | obtém uma sessão interativa (um TTY de verdade) |
| `lote logs <target> <handle>` | acompanha o log de uma execução |
| `lote info <target> <handle>` | post-mortem de um job (código de saída, memória, GPU) |
| `lote fetch <target> <path>` | faz rsync de um caminho de resultados de volta |
| `lote pull <handle>` | faz rsync de volta do caminho de resultados registrado no momento do submit |
| `lote watch <target>` | re-sincroniza o repositório a cada mudança em arquivo local |
| `lote history [limit]` | invocações recentes de comandos `lote` |

## setup e discover

```sh
lote discover miyabi          # onboard, then print the host's kind, root, and node classes
lote setup miyabi             # same onboarding, and start the pueue daemon
lote discover miyabi --wait 120   # give each queue's probe job at most two minutes
```

A integração encontra a raiz do repositório do host (uma área `/work` de HPC se houver, senão `~/projects`), faz rsync do repositório para lá, roda `chefe install` e então sonda o host em um login shell para descobrir seu scheduler, GPU e conta. Um host só é colocado em cache depois que o `chefe install` tem sucesso, então um que não consegue construir o ambiente nunca se torna um alvo.

Em um host com scheduler, a descoberta então mapeia as classes de nó. Ela pergunta ao scheduler suas filas (`qstat -q` no PBS, `sinfo` no SLURM) e submete um job mínimo por fila que imprime o snapshot de máquina do [mainboard](https://phvv.me/mainboard), então a GPU, a memória e os núcleos reais de cada fila ficam em cache sob aquela chave de classe, incluindo classes especiais como os movedores `prepost` do Miyabi. Uma fila que rejeita o job ou continua ocupada além de `--wait` é pulada com um aviso e recuperada pelo próximo discover.

## submit

```sh
lote submit miyabi train.sh                       # to a named host
lote submit miyabi train.sh --fetch results/run1  # remember a results path for `pull`
lote submit auto train.sh --needs 40              # route by free memory, pick smallest fit
```

O `submit` faz rsync do repositório, escolhe o scheduler do alvo e lança o script, registrando o git sha (e uma flag de dirty) junto ao handle. Com `auto`, `--needs <GB>` é obrigatório e o lote escolhe o host de menor memória que ainda comporta o job, mantendo as máquinas grandes livres. Argumentos posicionais extras depois do script chegam ao job pela variável de ambiente `ARGS`.

## ps, status e reconcile

```sh
lote ps                       # the last runs across every machine
lote status miyabi            # what is live on one host right now
lote reconcile miyabi         # recorded runs vs the live scheduler
```

O `ps` lê o armazenamento de estado local, então funciona offline. O `status` abre uma conexão ssh e pergunta diretamente ao scheduler do host. O `reconcile` junta os dois, dando a cada execução registrada um estado ao vivo, código de saída e um veredito de uma palavra (ok, failed, running ou vanished). Esse é o auxílio de depuração de estado local que substitui ficar atualizando uma fila à mão.

## logs, info, fetch e pull

```sh
lote logs miyabi <handle> --follow    # tail the merged stdout+stderr
lote info miyabi <handle>             # exit status, memory used vs cap, GPU usage
lote fetch miyabi research/out        # pull an arbitrary path back
lote pull <handle>                    # pull the path recorded at submit time
```

O `fetch` recebe um caminho relativo à raiz do repositório e faz rsync dele de volta para o mesmo caminho local. O `pull` é o atalho para quando você passou `--fetch` no momento do submit, então você não precisa redigitar o caminho.

## interact e watch

```sh
lote interact miyabi --gpus 2 --hours 4   # interactive session (qsub -I on pbs, ssh -t otherwise)
lote interact miyabi --dry-run            # print the command instead of running it
lote watch miyabi                         # re-rsync on every local file change, ctrl-c to stop
```

Em um host pbs, o `interact` submete um `qsub -I` interativo com a conta e a fila descobertas. Em qualquer outro host, ele abre um login shell. O `watch` mantém um host em sincronia com sua árvore de trabalho enquanto você itera, respeitando a mesma allowlist e o mesmo gitignore que o `submit`.

## exec

O executor on-host. O lote o chama via ssh, mas você também pode rodá-lo diretamente em um login node.

| comando | o que faz |
|---|---|
| `lote exec qsub <script> [args…]` | submete ao pbs, retorna o id do job |
| `lote exec sbatch <script> [args…]` | submete ao slurm, retorna o id do job |
| `lote exec run <script> [args…]` | roda via bash, sem scheduler |
| `lote exec status` | uma tabela dos meus jobs ativos |
| `lote exec info <jid>` | registro post-mortem de um job |
| `lote exec logs <jid\|name> [--follow]` | acompanha o log correspondente mais recente |
| `lote exec cancel <jid\|name\|all>` | `qdel` ou `scancel` |

```sh
lote exec qsub train.sh --select 2 --walltime 04:00:00
lote exec sbatch train.sh --gpus 4 --partition gpu
lote exec run smoke.sh                 # plain bash, off a cluster
lote exec status                       # squeue --me on slurm, otherwise qstat
lote exec cancel all                   # every job of mine
```

`qsub` e `sbatch` leem as diretivas `#PBS` ou `#SBATCH` do script e deixam as flags sobrescrevê-las, então um único script carrega defaults sensatos. Um nome de script sem caminho é resolvido nos diretórios `jobs/` do experimento, e `status`, `logs` e `info` escolhem o scheduler do host automaticamente.
