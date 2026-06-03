# Configuración

lote lee tres cosas. `lote.toml` dice qué enviar, `~/.ssh/config` dice dónde y `.lote/db.json` recuerda qué pasó. Todo lo demás sobre un host se autodescubre cuando lo integras, así que la mayoría de las configuraciones nunca tocan más que la tabla `[sync]`.

## lote.toml

El `lote.toml` en la raíz del repo es esencialmente solo el alcance de rsync. `include` es la lista de permitidos de rutas que un nodo de cómputo necesita, y `exclude` omite patrones pesados o regenerables dentro de ellas.

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

Las rutas de `include` se preservan con `rsync -R`, así que el árbol aterriza en el mismo lugar en el host. Los patrones de `exclude` se apilan sobre tu gitignore, que lote ya respeta, así que solo enumeras aquí los artefactos pesados adicionales. `lote watch` observa el mismo conjunto `include` y re-sincroniza cuando cualquier archivo no ignorado en él cambia.

### Destinos y pistas

Ambos son anulaciones opcionales para usuarios avanzados. Por defecto los destinos vienen de `~/.ssh/config` y cada capacidad se sondea, así que rara vez los configuras.

```toml
targets = ["miyabi", "pegasus", "dgx-spark"]   # restrict to these aliases

[hints.dgx-spark]                              # override a probed field
root = "/data/projects"
```

Una lista `targets` limita lote a alias específicos en lugar de cada host en tu configuración ssh. Una tabla `[hints.<alias>]` anula un campo sondeado para un host, siendo sus claves los campos del destino (`root`, `kind`, `gpu_mem_mb`, `account`, `queue`, etc.).

## destinos ssh

Los destinos de lote son los alias concretos `Host` en `~/.ssh/config`. Las entradas de patrón como `Host *` se ignoran, dejando los destinos reales y conectables.

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

Como los destinos son alias de ssh, todo lo que ya configuras para ssh simplemente funciona, incluyendo `ProxyJump`, archivos de identidad y multiplexación de conexiones. lote reutiliza una conexión por comando. No hay agente que instalar en el host ni daemon de control que ejecutar, solo ssh y, en cada host, [chefe](https://phvv.me/chefe) para que el ejecutor pueda construir y entrar al entorno.

## .lote/db.json

El estado de las ejecuciones vive en un único archivo [TinyDB](https://tinydb.readthedocs.io) en `.lote/db.json`. Guarda tres cosas, todas escritas desde tu laptop.

| almacén | qué recuerda |
|---|---|
| datos de hosts | el planificador sondeado, la raíz del repo, la GPU y la cuenta de cada host integrado |
| registro de ejecuciones | cada despacho, con su identificador, destino, script, sha de git y ruta `--fetch` |
| historial de comandos | cada invocación de `lote`, con marca de tiempo y su resultado |

`lote ls` lee los datos de host en caché sin volver a sondear. `lote ps` y `lote pull` leen el registro de ejecuciones, así que funcionan sin conexión. `lote history` lee el registro de comandos, y un `.lote/lote.log` hermano mantiene un rastro de texto rotado. Define `LOTE_NO_HISTORY=1` para omitir el registro. La integración escribe los datos de un host solo después de que `chefe install` tiene éxito, así que el registro solo nombra máquinas que realmente pueden ejecutar tus trabajos.
