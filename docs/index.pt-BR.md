# lote

Um único plano de comando para suas máquinas.

Um DGX via ssh, um cluster slurm, um supercomputador pbs, a máquina sobrando embaixo da mesa. Cada um roda jobs do seu próprio jeito, então o mesmo experimento precisa de `pueue` aqui, `sbatch` ali e `qsub` em outro lugar. O **lote** é o plano que fica acima de todos eles. Você escolhe uma máquina, ele faz rsync do seu repositório até lá, roda seu job sob qualquer scheduler que aquele host tenha e traz os resultados de volta. Um único script de job roda em qualquer lugar.

<div class="grid cards" markdown>

- :material-rocket-launch-outline: **Um único submit**

    `lote submit <host> job.sh` envia o repositório, lança o job e imprime um handle. Não importa o scheduler.

- :material-transit-connection-variant: **Qualquer scheduler**

    O lote detecta a fila de cada host e se adapta. `pueue` em uma máquina comum, `sbatch` no slurm, `qsub` no pbs.

- :material-radar: **Estado entre hosts**

    `lote ps` e `lote reconcile` acompanham cada execução em cada máquina, a partir do seu laptop.

- :material-lan-connect: **Apenas ssh**

    Os alvos são os aliases de host do seu `~/.ssh/config`. Nenhum agente para instalar, nenhum daemon de controle para cuidar.

</div>

## Instalação

```sh
pip install lote            # the lote command, ready to use
chefe add lote --pypi       # or pull it into a chefe project
```

O lote despacha seu executor on-host via ssh como `chefe run lote exec ...`, então cada host precisa do [chefe](https://phvv.me/chefe) (a ferramenta de ambiente irmã), que o `lote setup` instala e que sobe o pixi na primeira execução.

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

!!! warning "O lote é recente (`0.0.x`)"
    O formato de configuração e os comandos ainda podem mudar.

## A seguir

<div class="grid cards" markdown>

- [:material-cogs: **Como funciona**](how-it-works.md) — o pipeline de dispatch e suas três camadas.
- [:material-console: **Comandos**](commands.md) — a CLI completa.
- [:material-tune: **Configuração**](configuration.md) — `lote.toml`, alvos ssh e o armazenamento de estado.
- [:material-test-tube: **Exemplos**](examples.md) — fluxos de trabalho reais de ponta a ponta.

</div>
