from __future__ import annotations

from plumbum.machines.local import LocalMachine
from plumbum.machines.ssh_machine import SshMachine

# A plumbum machine the client wrappers run commands on, as ``machine["cmd"][args]``:
# ``local`` (the default) or an ``SshMachine`` for a remote host.
type Machine = LocalMachine | SshMachine
