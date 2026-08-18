import os
import signal
from contextlib import suppress

from plumbum.machines.local import LocalMachine
from plumbum.machines.session import ShellSession
from plumbum.machines.ssh_machine import SshMachine


class BoundedShellSession(ShellSession):
    """Close the dedicated SSH process group, including ProxyJump children."""

    def close(self) -> None:
        process = self.proc
        if process is not None and process.poll() is None:
            with suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGTERM)
        super().close()


class BoundedSshMachine(SshMachine):
    """An SSH machine whose session owns its entire local transport group."""

    def session(self, isatty: bool = False, *, new_session: bool = False) -> ShellSession:
        return BoundedShellSession(
            self.popen(["/bin/sh"], (["-tt"] if isatty else ["-T"]), new_session=new_session),
            self.custom_encoding,
            isatty,
            self.connect_timeout,
            host=self.host,
        )


# A plumbum machine the client wrappers run commands on, as ``machine["cmd"][args]``:
# ``local`` (the default) or an ``SshMachine`` for a remote host.
type Machine = LocalMachine | BoundedSshMachine
