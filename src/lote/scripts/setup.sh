#!/usr/bin/env bash
# Prepare a freshly-synced host: ensure chefe, build the env, start the pueue daemon.
# Usage: setup.sh <repo-root>.  Run by `lote setup` after the repo is rsynced.
set -euo pipefail
cd "$1"

# Put the per-user install dirs on PATH first, so an already-installed chefe (and
# the pixi/cargo engines it manages) is found before we ever try to install it.
export PATH="$HOME/.local/bin:$HOME/.pixi/bin:$HOME/.cargo/bin:$PATH"

# chefe owns the environment and installs its own pixi engine on the first
# `chefe install`, so a single user-level install is all a fresh host needs.
# `--break-system-packages` keeps pip working on PEP 668 externally-managed pythons.
command -v chefe >/dev/null 2>&1 || python3 -m pip install --user --break-system-packages chefe
chefe install

# Start the pueue daemon that ssh-target dispatch uses. pueue ships in the env
# (chefe.toml [deps]), so run it through the compiled manifest.
chefe run pueued -d >/dev/null 2>&1 || true
