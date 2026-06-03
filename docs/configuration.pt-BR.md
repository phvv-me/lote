# Configuração

O lote lê três coisas. O `lote.toml` diz o que enviar, o `~/.ssh/config` diz para onde, e o `.lote/db.json` lembra o que aconteceu. Todo o resto sobre um host é auto-descoberto quando você o integra, então a maioria das configurações nunca mexe além da tabela `[sync]`.

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

### Alvos e hints

Ambos são overrides opcionais para usuários avançados. Por padrão, os alvos vêm do `~/.ssh/config` e toda capacidade é sondada, então você raramente define isto.

```toml
targets = ["miyabi", "pegasus", "dgx-spark"]   # restrict to these aliases

[hints.dgx-spark]                              # override a probed field
root = "/data/projects"
```

Uma lista `targets` restringe o lote a aliases específicos em vez de todo host no seu ssh config. Uma tabela `[hints.<alias>]` sobrescreve um campo sondado para um host, sendo suas chaves os campos do alvo (`root`, `kind`, `gpu_mem_mb`, `account`, `queue` e assim por diante).

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

## .lote/db.json

O estado das execuções vive em um único arquivo [TinyDB](https://tinydb.readthedocs.io) em `.lote/db.json`. Ele guarda três coisas, todas gravadas a partir do seu laptop.

| store | o que ele lembra |
|---|---|
| fatos do host | o scheduler sondado, a raiz do repositório, a GPU e a conta de cada host integrado |
| registro de execuções | cada dispatch, com seu handle, alvo, script, git sha e caminho de `--fetch` |
| histórico de comandos | cada invocação do `lote`, cronometrada, com seu resultado |

O `lote ls` lê os fatos do host em cache sem sondar de novo. O `lote ps` e o `lote pull` leem o registro de execuções, então funcionam offline. O `lote history` lê o log de comandos, e um `.lote/lote.log` irmão mantém um trace de texto rotacionado. Defina `LOTE_NO_HISTORY=1` para pular o registro. A integração grava os fatos de um host apenas depois que o `chefe install` tem sucesso, então o registro só nomeia máquinas que de fato conseguem rodar seus jobs.
