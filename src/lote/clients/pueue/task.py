from __future__ import annotations

from ...base import Model
from .state import PueueState


class PueueTask(Model):
    """A task from ``pueue status --json``.

    id: pueue's task id (the lote handle for ssh targets). label: the submit label.
    state: lifecycle state. result: ``TaskResult`` once ``Done`` (``Success``,
        ``Failed``, ``Killed``, ...). exit_code: process code when ``Failed``.
    start: ISO start time.
    """

    id: int
    label: str | None = None
    state: PueueState | str
    result: str | None = None
    exit_code: int | None = None
    start: str | None = None

    @property
    def succeeded(self) -> bool:
        """True once the task finished with a ``Success`` result."""
        return self.state == PueueState.DONE and self.result == "Success"
