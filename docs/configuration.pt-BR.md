# Configuração

O lote lê três coisas. O `lote.toml` diz o que enviar, o `~/.ssh/config` diz para onde, e o `.lote/db.sqlite` lembra o que aconteceu. Todo o resto sobre um host é auto-descoberto quando você o integra, então a maioria das configurações nunca mexe além da tabela `[sync]`.

## lote.toml

O `lote.toml` na raiz do repositório é essencialmente apenas o escopo do rsync. `include` é a allowlist de caminhos que um compute node precisa, e `exclude` ignora padrões pesados ou regeneráveis dentro deles.

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

Os caminhos de `include` são preservados com `rsync -R`, então a árvore chega no mesmo lugar no host. Os padrões de `exclude` se somam ao seu gitignore, que o lote já respeita, então você só lista aqui os artefatos pesados extras. O `lote watch` observa o mesmo conjunto `include` e re-sincroniza quando qualquer arquivo não ignorado dentro dele muda.

A sincronização espelha a árvore local. Tudo dentro do conjunto `include` que não existe mais localmente é removido no host (`rsync --delete`), então um módulo renomeado nunca deixa uma cópia obsoleta para trás sombreando seu substituto. Artefatos que só existem no host sobrevivem através de `protect`, uma lista de padrões de filtro do rsync cujos padrões protegem `.lote/***`, `results/***`, `logs/***` e `*.log` (a forma `dir/***` casa com um diretório e tudo dentro dele, em qualquer profundidade). Caminhos excluídos também nunca são apagados. Definir `protect` substitui os padrões, então repita-os quando quiser apenas adicionar:

```toml
[sync]
protect = [".lote/***", "results/***", "logs/***", "*.log", "checkpoints/***"]
```
No macOS instale o rsync upstream (`brew install rsync`), pois o openrsync que a Apple embute não consegue podar em um host remoto e o lote se recusa a espelhar com ele.

### Alvos e hints

Ambos são overrides opcionais para usuários avançados. Por padrão, os alvos vêm do `~/.ssh/config` e toda capacidade é sondada, então você raramente define isto.

```toml
targets = ["miyabi", "pegasus", "dgx-spark"]   # restrict to these aliases

[hints.dgx-spark]                              # override a probed field
root = "/data/projects"
```

Uma lista `targets` restringe o lote a aliases específicos em vez de todo host no seu ssh config. Uma tabela `[hints.<alias>]` sobrescreve um campo sondado para um host, sendo suas chaves os campos de identidade do alvo (`root`, `kind`, `account`, `queue`, `login_shell`). As capacidades de hardware não aceitam hint porque são sondadas por classe de nó, como explica a próxima seção.

## alvos ssh

Os alvos do lote são os aliases de `Host` concretos no `~/.ssh/config`. Entradas de padrão como `Host *` são ignoradas, deixando os destinos reais e conectáveis.

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

Como os alvos são aliases ssh, tudo o que você já configura para ssh simplesmente funciona, incluindo `ProxyJump`, arquivos de identidade e multiplexação de conexão. O lote reutiliza uma conexão por comando. Não há agente para instalar no host nem daemon de controle para rodar, apenas ssh e, em cada host, o [chefe](https://phvv.me/chefe) para que o executor possa construir e entrar no ambiente.

## Classes de nó

Um host raramente é uma única máquina. O nó de login ssh é uma classe de nó, e cada fila do scheduler (uma fila PBS, uma partição SLURM) fica à frente do seu próprio hardware, então as filas de GPU de um host HPC não se parecem em nada com seu nó de login e filas especiais como os movedores de dados `prepost` do Miyabi são diferentes de novo. Por isso o lote mantém as capacidades por classe de nó.

A classe `login` vem da sondagem padrão sobre ssh, sem nada instalado no host. Toda outra classe é descoberta durante a integração perguntando ao scheduler sua lista de filas (`qstat -q` no PBS, `sinfo` no SLURM) e submetendo um job mínimo a cada fila, cuja única carga é imprimir o snapshot de máquina do [mainboard](https://phvv.me/mainboard). O snapshot interpretado vira a classe daquela fila, gravado em cache sob sua chave de classe à medida que os jobs de sondagem respondem, e o `lote ls` lista cada classe abaixo do seu host. Uma fila que rejeita o job, o falha ou fica ocupada além de `--wait` (600 segundos por padrão) é pulada com um aviso, nunca um discover falho. Um host ssh não tem filas, então sua integração continua sendo a única sondagem de login de sempre.

## .lote/db.sqlite

O estado das execuções vive em um único arquivo [SQLite](https://www.sqlite.org) em modo WAL em `.lote/db.sqlite`. O SQLite dá upserts seguros sob concorrência sem reescrever o arquivo inteiro, então chamadas paralelas do `lote` nunca corrompem o armazenamento. Ele guarda quatro coisas, todas gravadas a partir do seu laptop.

| store | o que ele lembra |
|---|---|
| fatos do host | o scheduler sondado, a raiz do repositório e a conta de cada host integrado |
| classes de nó | uma linha por chave `(host, classe)`, com a GPU, a memória e os núcleos sondados daquela classe |
| registro de execuções | cada dispatch, com seu handle, alvo, script, git sha e caminho de `--fetch` |
| histórico de comandos | cada invocação do `lote`, cronometrada, com seu resultado |

O `lote ls` lê os fatos e as classes em cache sem sondar de novo. O `lote ps` e o `lote pull` leem o registro de execuções, então funcionam offline. O `lote history` lê o log de comandos, e um `.lote/lote.log` irmão mantém um trace de texto rotacionado. Defina `LOTE_NO_HISTORY=1` para pular o registro. A integração grava os fatos de um host apenas depois que o `chefe install` tem sucesso, então o registro só nomeia máquinas que de fato conseguem rodar seus jobs.
