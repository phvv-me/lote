# lote

Un solo plano de comando para tus máquinas.

Un DGX por ssh, un clúster slurm, una supercomputadora pbs, la caja de repuesto debajo del escritorio. Cada una ejecuta trabajos a su manera, así que el mismo experimento necesita `pueue` aquí, `sbatch` allá y `qsub` en otro lado. **lote** es el plano que se sitúa sobre todas ellas. Eliges una máquina, hace rsync de tu repo allí, ejecuta tu trabajo bajo el planificador que tenga ese host y trae los resultados de vuelta. Un mismo script de trabajo corre en cualquier lugar.

<div class="grid cards" markdown>

- :material-rocket-launch-outline: **Un solo submit**

    `lote submit <host> job.sh` envía el repo, lanza el trabajo e imprime un identificador. Sin importar el planificador.

- :material-transit-connection-variant: **Cualquier planificador**

    lote detecta la cola de cada host y se adapta. `pueue` en una caja sencilla, `sbatch` en slurm, `qsub` en pbs.

- :material-radar: **Estado entre hosts**

    `lote ps` y `lote reconcile` rastrean cada ejecución en cada máquina desde tu laptop.

- :material-lan-connect: **Solo ssh**

    Los destinos son los alias de host de tu `~/.ssh/config`. Sin agente que instalar, sin daemon de control que cuidar.

</div>

## Instalación

```sh
pip install lote            # the lote command, ready to use
chefe add lote --pypi       # or pull it into a chefe project
```

lote despacha su ejecutor en host por ssh como `chefe run lote exec ...`, así que cada host necesita [chefe](https://phvv.me/chefe) (la herramienta hermana de entornos), que `lote setup` instala y que levanta pixi en la primera ejecución.

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

!!! warning "lote es temprano (`0.0.x`)"
    El formato de configuración y los comandos aún pueden cambiar.

## Siguiente

<div class="grid cards" markdown>

- [:material-cogs: **Cómo funciona**](how-it-works.md) — el pipeline de despacho y sus tres capas.
- [:material-console: **Comandos**](commands.md) — la CLI completa.
- [:material-tune: **Configuración**](configuration.md) — `lote.toml`, destinos ssh y el almacén de estado.
- [:material-test-tube: **Ejemplos**](examples.md) — flujos de trabajo reales de extremo a extremo.

</div>
