# Ejemplos

El recorrido más rápido por lote son unos cuantos flujos de trabajo reales. Cada uno es el mismo `train.sh` llegando a una máquina diferente, con lote adaptándose por debajo.

## Integrar un host nuevo

Apunta lote a cualquier alias de ssh y deja que descubra el resto.

```sh
lote ls                        # the ssh-config aliases lote can target
lote setup miyabi              # probe + rsync + chefe install, then start the queue
lote discover pegasus          # onboard and print what it is, without starting a queue
```

`setup` encuentra la raíz del repo, envía el código, construye el entorno con chefe y sondea el host en una shell de inicio de sesión para que la cadena de herramientas del clúster sea visible. Después de esto, lote sabe que miyabi es un host slurm y pegasus es pbs, y cada comando posterior se enruta por sí mismo.

## Despachar y seguir un trabajo

El bucle central. Enviar, lanzar, observar, traer.

```sh
lote submit miyabi train.sh --fetch research/out/run-42   # prints a handle, e.g. 1837492
lote status miyabi                                        # is it running yet?
lote logs miyabi 1837492 --follow                         # tail the merged output
lote info miyabi 1837492                                  # exit code, memory used, GPU
lote pull 1837492                                         # rsync research/out/run-42 back
```

`submit` registra el identificador con el sha de git con el que corrió, así que un resultado en disco siempre se rastrea de vuelta al commit exacto. Como pasaste `--fetch`, `pull` ya conoce la ruta y la hace rsync a casa sin volver a escribirla.

## Enrutar un trabajo por memoria

Deja que lote elija la máquina para un trabajo que necesita una cantidad conocida de memoria.

```sh
lote submit auto train.sh --needs 40      # smallest host with >= 40 GB free, ships and launches
```

lote considera cada host integrado, descarta los que no encajan en 40 GB y elige el más pequeño de los restantes, así que un trabajo pequeño no ocupa una máquina grande. Si nada encaja, te dice qué tiene cada host.

## Iterar en vivo en un host remoto

Mantén un host al unísono con tu editor mientras depuras.

```sh
lote watch dgx-spark           # re-rsync on every save, ctrl-c to stop
# in another shell:
lote submit dgx-spark smoke.sh    # the host already has your latest code
```

`watch` respeta la misma lista de permitidos `[sync]` y gitignore que `submit`, así que solo viajan los archivos que un nodo de cómputo necesita, y solo cuando cambian. En una caja sencilla como dgx-spark el trabajo va a `pueue`, el mismo `smoke.sh` que llegaría a `sbatch` en miyabi.

## Rastrear cada host

Ve todo a la vez, desde tu laptop, sin conexión.

```sh
lote ps                        # the last runs across every machine, from the state store
lote reconcile miyabi          # recorded runs vs the live scheduler, with a verdict each
lote history                   # the recent lote commands you ran
```

`ps` y `history` leen `.lote/db.json`, así que responden al instante sin tocar un host. `reconcile` abre una conexión ssh, pregunta al planificador por cada ejecución registrada y etiqueta cada una como ok, failed, running o vanished, que es como notas un trabajo que murió sin un correo.

## Ejecutar el ejecutor a mano

En un nodo de inicio de sesión puedes manejar el ejecutor directamente, el mismo código que lote llama por ssh.

```sh
lote exec sbatch train.sh --gpus 4         # submit here, return the job id
lote exec status                           # my live jobs, squeue or qstat as fits
lote exec logs train --follow              # tail by job name, not just id
lote exec cancel all                       # stop every job of mine
```

Esto es útil cuando ya estás en el clúster, o cuando depuras lo que lote ejecutaría de forma remota. Las directivas en `train.sh` establecen los valores predeterminados, y las banderas los anulan.
