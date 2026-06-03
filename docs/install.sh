#!/bin/sh
# fleetctl installer. Installs the `fleet` command without needing uv or a
# pre-existing Python: pixi provides the Python, pip installs fleetctl into an
# isolated venv. fleet drives hosts via `chefe run ...`, so chefe is ensured too.
# Needs only curl. Run with:  curl -fsSL https://phvv.me/fleetctl/install.sh | sh
set -eu

say() { printf '\033[36mfleetctl\033[0m %s\n' "$1"; }

# pixi provides the Python fleetctl is installed with — no system Python needed.
if ! command -v pixi >/dev/null 2>&1; then
  say "installing pixi"
  curl -fsSL https://pixi.sh/install.sh | sh
fi
PIXI_BIN="${PIXI_HOME:-$HOME/.pixi}/bin"
export PATH="$PIXI_BIN:$HOME/.local/bin:$PATH"

# fleet drives hosts through chefe; ensure it is present before installing fleet.
if ! command -v chefe >/dev/null 2>&1; then
  say "installing chefe (fleet drives hosts via 'chefe run ...')"
  curl -fsSL https://phvv.me/chefe/install.sh | sh
fi

# fleetctl is a Python CLI. Build an isolated venv with pixi's own Python and
# pip fleetctl into it — pixi is the only dependency.
say "installing fleetctl"
pixi global install "python>=3.12" >/dev/null
FLEET_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/fleetctl"
"$PIXI_BIN/python" -m venv "$FLEET_HOME"
"$FLEET_HOME/bin/pip" install --quiet --upgrade pip fleetctl
mkdir -p "$HOME/.local/bin"
ln -sf "$FLEET_HOME/bin/fleet" "$HOME/.local/bin/fleet"

say "done — ensure ~/.local/bin is on your PATH, then run 'fleet --help'"
