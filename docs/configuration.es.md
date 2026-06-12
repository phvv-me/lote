# Configuración

lote lee tres cosas. `lote.toml` dice qué enviar, `~/.ssh/config` dice dónde y `.lote/db.sqlite` recuerda qué pasó. Todo lo demás sobre un host se autodescubre cuando lo integras, así que la mayoría de las configuraciones nunca tocan más que la tabla `[sync]`.

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

La sincronización refleja el árbol local. Todo lo que esté dentro del conjunto `include` y ya no exista localmente se poda en el host (`rsync --delete`), así que un módulo renombrado nunca deja atrás una copia obsoleta que oculte a su reemplazo. Los artefactos que solo existen en el host sobreviven gracias a `protect`, una lista de patrones de filtro de rsync cuyos valores por defecto protegen `.lote/***`, `results/***`, `logs/***` y `*.log` (la forma `dir/***` coincide con un directorio y todo lo que contiene, a cualquier profundidad). Las rutas excluidas tampoco se borran nunca. Definir `protect` reemplaza los valores por defecto, así que repítelos cuando solo quieras añadir:

```toml
[sync]
protect = [".lote/***", "results/***", "logs/***", "*.log", "checkpoints/***"]
```
En macOS instala el rsync upstream (`brew install rsync`), ya que el openrsync que Apple incluye no puede podar en un host remoto y lote se niega a espejar con él.

### Destinos y pistas

Ambos son anulaciones opcionales para usuarios avanzados. Por defecto los destinos vienen de `~/.ssh/config` y cada capacidad se sondea, así que rara vez los configuras.

```toml
targets = ["miyabi", "pegasus", "dgx-spark"]   # restrict to these aliases

[hints.dgx-spark]                              # override a probed field
root = "/data/projects"
```

Una lista `targets` limita lote a alias específicos en lugar de cada host en tu configuración ssh. Una tabla `[hints.<alias>]` anula un campo sondeado para un host, siendo sus claves los campos de identidad del destino (`root`, `kind`, `account`, `queue`, `login_shell`). Las capacidades de hardware no se pueden fijar con hints porque se sondean por clase de nodo, como explica la siguiente sección.

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

## Clases de nodo

Un host rara vez es una sola máquina. El nodo de login ssh es una clase de nodo, y cada cola del planificador (una cola PBS, una partición SLURM) tiene delante su propio hardware, así que las colas de GPU de un host HPC no se parecen en nada a su nodo de login y las colas especiales como los movedores de datos `prepost` de Miyabi son distintas otra vez. Por eso lote guarda las capacidades por clase de nodo.

La clase `login` viene del sondeo estándar sobre ssh, sin nada instalado en el host. Cada otra clase se descubre durante la integración pidiendo al planificador su lista de colas (`qstat -q` en PBS, `sinfo` en SLURM) y enviando un trabajo mínimo a cada cola, cuya única carga es imprimir la instantánea de máquina de [mainboard](https://phvv.me/mainboard). La instantánea interpretada se convierte en la clase de esa cola, guardada bajo su clave de clase a medida que los trabajos de sondeo responden, y `lote ls` lista cada clase debajo de su host. Una cola que rechaza el trabajo, lo falla o sigue ocupada más allá de `--wait` (600 segundos por defecto) se omite con un aviso, nunca un discover fallido. Un host ssh no tiene colas, así que su integración sigue siendo el único sondeo de login de siempre.

## .lote/db.sqlite

El estado de las ejecuciones vive en un único archivo [SQLite](https://www.sqlite.org) en modo WAL en `.lote/db.sqlite`. SQLite da upserts seguros ante concurrencia sin reescribir el archivo entero, así que las llamadas paralelas de `lote` nunca corrompen el almacén. Guarda cuatro cosas, todas escritas desde tu laptop.

| almacén | qué recuerda |
|---|---|
| datos de hosts | el planificador sondeado, la raíz del repo y la cuenta de cada host integrado |
| clases de nodo | una fila por clave `(host, clase)`, con la GPU, la memoria y los núcleos sondeados de esa clase |
| registro de ejecuciones | cada despacho, con su identificador, destino, script, sha de git y ruta `--fetch` |
| historial de comandos | cada invocación de `lote`, con marca de tiempo y su resultado |

`lote ls` lee los datos y las clases en caché sin volver a sondear. `lote ps` y `lote pull` leen el registro de ejecuciones, así que funcionan sin conexión. `lote history` lee el registro de comandos, y un `.lote/lote.log` hermano mantiene un rastro de texto rotado. Define `LOTE_NO_HISTORY=1` para omitir el registro. La integración escribe los datos de un host solo después de que `chefe install` tiene éxito, así que el registro solo nombra máquinas que realmente pueden ejecutar tus trabajos.
