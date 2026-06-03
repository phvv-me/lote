#!/usr/bin/env bash
# Prepare a freshly-synced host: build the chefe env and start the pueue daemon.
# Usage: setup.sh <repo-root>.  Run by `lote setup` after the repo is rsynced.
set -euo pipefail
cd "$1"

# chefe owns the environment. (Re)install it and its pixi engine via the chefe
# installer — idempotent, and it upgrades chefe to the latest — then build.
curl -fsSL https://phvv.me/chefe/install.sh | sh
export PATH="$HOME/.local/bin:$HOME/.pixi/bin:$HOME/.cargo/bin:$PATH"
chefe install

# Start the pueue daemon that ssh-target dispatch uses. pueue ships in the env
# now (chefe.toml [deps]), so run it through the compiled manifest.
chefe run pueued -d >/dev/null 2>&1 || true
