# Comandos

lote tiene dos grupos de comandos. Los verbos de nivel superior `lote` son el plano de control que ejecutas en tu laptop, y el grupo `lote exec` es el ejecutor en host que corre en una sola máquina. Los destinos son alias de host de `~/.ssh/config` en todo momento.

## Plano de control

| comando | qué hace |
|---|---|
| `lote ls` | lista los destinos de ssh-config con sus capacidades en caché (nunca sondea) |
| `lote discover <target>` | integra un host y mapea sus clases de nodo (probe + sync + `chefe install` + un trabajo de sondeo por cola) |
| `lote setup <target>` | integra un host e inicia el daemon de su cola |
| `lote submit <target\|auto> <script> [args…]` | hace rsync del repo hacia arriba y lanza `script`, imprime un identificador |
| `lote ps [limit]` | ejecuciones despachadas recientes en cada destino |
| `lote status <target>` | trabajos en vivo en un destino |
| `lote reconcile <target>` | compara las ejecuciones registradas de un destino con el planificador en vivo |
| `lote interact <target>` | obtiene una sesión interactiva (un TTY real) |
| `lote logs <target> <handle>` | sigue el log de una ejecución |
| `lote info <target> <handle>` | el post-mortem de un trabajo (código de salida, memoria, GPU) |
| `lote fetch <target> <path>` | hace rsync de una ruta de resultados de vuelta |
| `lote pull <handle>` | hace rsync de vuelta de la ruta de resultados registrada al hacer submit |
| `lote watch <target>` | re-sincroniza el repo en cada cambio de archivo local |
| `lote history [limit]` | invocaciones recientes de comandos `lote` |

## setup y discover

```sh
lote discover miyabi          # onboard, then print the host's kind, root, and node classes
lote setup miyabi             # same onboarding, and start the pueue daemon
lote discover miyabi --wait 120   # give each queue's probe job at most two minutes
```

La integración encuentra la raíz del repo del host (un área `/work` de HPC si la hay, si no `~/projects`), hace rsync del repo allí, ejecuta `chefe install`, luego sondea el host en una shell de inicio de sesión para su planificador, GPU y cuenta. Un host se guarda en caché solo después de que `chefe install` tiene éxito, así que uno que no puede construir el entorno nunca se convierte en destino.

En un host con planificador, el descubrimiento mapea entonces las clases de nodo. Pide al planificador sus colas (`qstat -q` en PBS, `sinfo` en SLURM) y envía un trabajo mínimo por cola que imprime la instantánea de máquina de [mainboard](https://phvv.me/mainboard), así la GPU, la memoria y los núcleos reales de cada cola quedan en caché bajo esa clave de clase, incluyendo clases especiales como los movedores `prepost` de Miyabi. Una cola que rechaza el trabajo o sigue ocupada más allá de `--wait` se omite con un aviso y la recoge el siguiente discover.

## submit

```sh
lote submit miyabi train.sh                       # to a named host
lote submit miyabi train.sh --fetch results/run1  # remember a results path for `pull`
lote submit auto train.sh --needs 40              # route by free memory, pick smallest fit
```

`submit` hace rsync del repo, elige el planificador del destino y lanza el script, registrando el sha de git (y una bandera de cambios sin confirmar) junto al identificador. Con `auto`, `--needs <GB>` es obligatorio y lote elige el host de menor memoria que aún encaje, manteniendo libres las máquinas grandes. Los argumentos posicionales extra después del script llegan al trabajo mediante la variable de entorno `ARGS`.

## ps, status y reconcile

```sh
lote ps                       # the last runs across every machine
lote status miyabi            # what is live on one host right now
lote reconcile miyabi         # recorded runs vs the live scheduler
```

`ps` lee el almacén de estado local, así que funciona sin conexión. `status` abre una conexión ssh y pregunta directamente al planificador del host. `reconcile` une ambos, dando a cada ejecución registrada un estado en vivo, código de salida y un veredicto de una palabra (ok, failed, running o vanished). Esta es la ayuda de depuración de estado local que reemplaza refrescar una cola a mano.

## logs, info, fetch y pull

```sh
lote logs miyabi <handle> --follow    # tail the merged stdout+stderr
lote info miyabi <handle>             # exit status, memory used vs cap, GPU usage
lote fetch miyabi research/out        # pull an arbitrary path back
lote pull <handle>                    # pull the path recorded at submit time
```

`fetch` toma una ruta relativa a la raíz del repo y la hace rsync de vuelta a la misma ruta local. `pull` es el atajo cuando pasaste `--fetch` al hacer submit, así que no vuelves a escribir la ruta.

## interact y watch

```sh
lote interact miyabi --gpus 2 --hours 4   # interactive session (qsub -I on pbs, ssh -t otherwise)
lote interact miyabi --dry-run            # print the command instead of running it
lote watch miyabi                         # re-rsync on every local file change, ctrl-c to stop
```

En un host pbs, `interact` envía un `qsub -I` interactivo con la cuenta y la cola descubiertas. En cualquier otro host abre una shell de inicio de sesión. `watch` mantiene un host en sincronía con tu árbol de trabajo mientras iteras, respetando la misma lista de permitidos y gitignore que `submit`.

## exec

El ejecutor en host. lote lo llama por ssh, pero también puedes ejecutarlo directamente en un nodo de inicio de sesión.

| comando | qué hace |
|---|---|
| `lote exec qsub <script> [args…]` | envía a pbs, devuelve el id del trabajo |
| `lote exec sbatch <script> [args…]` | envía a slurm, devuelve el id del trabajo |
| `lote exec run <script> [args…]` | ejecuta a través de bash, sin planificador |
| `lote exec status` | una tabla de mis trabajos en vivo |
| `lote exec info <jid>` | el registro post-mortem de un trabajo |
| `lote exec logs <jid\|name> [--follow]` | sigue el log coincidente más reciente |
| `lote exec cancel <jid\|name\|all>` | `qdel` o `scancel` |

```sh
lote exec qsub train.sh --select 2 --walltime 04:00:00
lote exec sbatch train.sh --gpus 4 --partition gpu
lote exec run smoke.sh                 # plain bash, off a cluster
lote exec status                       # squeue --me on slurm, otherwise qstat
lote exec cancel all                   # every job of mine
```

`qsub` y `sbatch` leen las directivas `#PBS` o `#SBATCH` del script y dejan que las banderas las anulen, así que un solo script lleva valores predeterminados sensatos. Un nombre de script sin ruta se resuelve contra los directorios `jobs/` del experimento, y `status`, `logs` e `info` eligen automáticamente el planificador del host.
