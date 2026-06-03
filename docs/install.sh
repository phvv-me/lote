#!/bin/sh
# lote installer. Installs the `lote` command without needing uv or a
# pre-existing Python: pixi provides the Python, pip installs lote into an
# isolated venv. lote drives hosts via `chefe run ...`, so chefe is ensured too.
# Needs only curl. Run with:  curl -fsSL https://phvv.me/lote/install.sh | sh
set -eu

say() { printf '\033[36mlote\033[0m %s\n' "$1"; }

# pixi provides the Python lote is installed with — no system Python needed.
if ! command -v pixi >/dev/null 2>&1; then
  say "installing pixi"
  curl -fsSL https://pixi.sh/install.sh | sh
fi
PIXI_BIN="${PIXI_HOME:-$HOME/.pixi}/bin"
export PATH="$PIXI_BIN:$HOME/.local/bin:$PATH"

# lote drives hosts through chefe; ensure it is present before installing lote.
if ! command -v chefe >/dev/null 2>&1; then
  say "installing chefe (lote drives hosts via 'chefe run ...')"
  curl -fsSL https://phvv.me/chefe/install.sh | sh
fi

# lote is a Python CLI. Build an isolated venv with pixi's own Python and
# pip lote into it — pixi is the only dependency.
say "installing lote"
pixi global install "python>=3.12" >/dev/null
LOTE_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/lote"
"$PIXI_BIN/python" -m venv "$LOTE_HOME"
"$LOTE_HOME/bin/pip" install --quiet --upgrade pip lote
mkdir -p "$HOME/.local/bin"
ln -sf "$LOTE_HOME/bin/lote" "$HOME/.local/bin/lote"

say "done — ensure ~/.local/bin is on your PATH, then run 'lote --help'"
