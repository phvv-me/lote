#!/usr/bin/env bash
# Prepare a freshly-synced host: ensure chefe, build the env, start the pueue daemon.
# Usage: setup.sh <repo-root>.  Run by `lote setup` after the repo is rsynced.
set -euo pipefail
cd "$1"

# Put the per-user install dirs on PATH first, so an already-installed chefe (and
# the pixi/cargo engines it manages) is found before we ever try to install it.
export PATH="$HOME/.local/bin:$HOME/.pixi/bin:$HOME/.cargo/bin:$PATH"

# chefe owns the environment and installs its own pixi engine on the first `chefe install`.
# Install it if missing AND keep it current: a host's older chefe must not choke on a manifest
# that uses a newer feature (chefe's `[workspace] min-chefe` floor) -- the exact failure where a
# stale remote chefe rejected a new manifest. `--upgrade` is idempotent and onboarding is
# occasional, so the network cost is fine; `--break-system-packages` keeps pip working on the
# PEP 668 externally-managed pythons.
python3 -m pip install --user --break-system-packages --upgrade chefe
chefe install

# Start the pueue daemon that ssh-target dispatch uses. pueue ships in the env
# (chefe.toml [deps]), so run it through the compiled manifest.
chefe run pueued -d >/dev/null 2>&1 || true
