"""Runner adapter (§6).

Local mode is implemented completely: the portal worker claims queued
executions against registered runners honoring capacity, allowed projects,
drain state, leases, and heartbeats. Cancellation targets the actual
process tree via the KRE adapter.

Remote transport: the architecture decision is a secured Python agent
(option A in §6) — NOT pretending KRE exposes a remote HTTP service.
Status: not implemented; remote runners stay `not-configured` until the
agent lands. A registered remote-mode runner simply never gets assigned.
"""


class RunnerAdapter:
    """Capacity/eligibility helpers; scheduling itself lives in dispatch."""

    def __init__(self, runner) -> None:
        self.runner = runner

    def is_local(self) -> bool:
        return self.runner.connection_mode == "local"

    def assignable(self) -> bool:
        """Only local runners can receive work today — remote transport is
        an explicit pending architecture item, not a stub pretending."""
        return (
            self.runner.enabled
            and not self.runner.draining
            and self.is_local()
        )
