#!/usr/bin/env bash
# Prepare a freshly-synced host: ensure chefe, build the env, start the pueue daemon.
# Usage: setup.sh <repo-root>.  Run by `lote setup` after the repo is rsynced.
set -euo pipefail
cd "$1"

# Put the per-user install dirs on PATH first, so an already-installed chefe (and
# the pixi/cargo engines it manages) is found before we ever try to install it.
export PATH="$HOME/.local/bin:$HOME/.pixi/bin:$HOME/.cargo/bin:$PATH"

# chefe owns the environment and installs its own pixi engine on the first `chefe install`.
# The synced repo carries chefe's own source, so prefer installing chefe from it: the
# manifest and the tool then can never drift apart (a stale remote chefe rejecting a newer
# manifest is exactly the failure this prevents). Without the source, fall back to a PyPI
# upgrade where an installer exists; tolerate upgrade failure when chefe already runs.
if [ -d packages/chefe ] && command -v uv >/dev/null 2>&1; then
  uv tool install --force -e packages/chefe
elif [ -d packages/chefe ] && python3 -m pip --version >/dev/null 2>&1; then
  python3 -m pip install --user --break-system-packages --force-reinstall -e packages/chefe
elif command -v uv >/dev/null 2>&1; then
  uv tool install --upgrade chefe || true
elif python3 -m pip --version >/dev/null 2>&1; then
  python3 -m pip install --user --break-system-packages --upgrade chefe || true
fi
if ! command -v chefe >/dev/null 2>&1; then
  echo "setup.sh: chefe is not installed and no installer (uv or pip) is available" >&2
  exit 1
fi
# Discovery deliberately syncs newer resolution inputs onto an existing host. Resolve that
# expected drift here, then let chefe's own verified locked pass complete the installation.
chefe install --resolve

# Start the pueue daemon that ssh-target dispatch uses. pueue ships in the env
# (chefe.toml [deps]), so run it through the compiled manifest.
chefe run pueued -d >/dev/null 2>&1 || true
