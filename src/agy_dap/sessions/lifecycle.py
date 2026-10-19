"""Session state machine, transition rules, and stop-generation tracking."""

from __future__ import annotations

from enum import Enum

from agy_dap.errors import DapPluginError, ErrorCode


class SessionState(str, Enum):
    """Explicit lifecycle states for a DAP debugging session."""

    CREATED = "CREATED"
    STARTING = "STARTING"
    CONFIGURING = "CONFIGURING"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    TERMINATING = "TERMINATING"
    TERMINATED = "TERMINATED"


ALLOWED_TRANSITIONS: dict[SessionState, set[SessionState]] = {
    SessionState.CREATED: {
        SessionState.STARTING,
        SessionState.TERMINATING,
        SessionState.TERMINATED,
    },
    SessionState.STARTING: {
        SessionState.CONFIGURING,
        SessionState.TERMINATING,
        SessionState.TERMINATED,
    },
    SessionState.CONFIGURING: {
        SessionState.RUNNING,
        SessionState.STOPPED,
        SessionState.TERMINATING,
        SessionState.TERMINATED,
    },
    SessionState.RUNNING: {
        SessionState.STOPPED,
        SessionState.TERMINATING,
        SessionState.TERMINATED,
    },
    SessionState.STOPPED: {
        SessionState.RUNNING,
        SessionState.TERMINATING,
        SessionState.TERMINATED,
    },
    SessionState.TERMINATING: {
        SessionState.TERMINATED,
    },
    SessionState.TERMINATED: {
        SessionState.TERMINATED,
    },
}


class LifecycleTracker:
    """Enforces valid session state transitions and manages stop-generation IDs."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.state: SessionState = SessionState.CREATED
        self.stop_generation: int = 0
        self.current_snapshot_id: str | None = None
        self.stale_snapshot_ids: set[str] = set()
        self.stale_frame_ids: set[int] = set()
        self.current_frame_ids: set[int] = set()

    def transition_to(self, target: SessionState) -> SessionState:
        """Transition to target state or raise INVALID_STATE_TRANSITION."""
        allowed = ALLOWED_TRANSITIONS.get(self.state, set())
        if target not in allowed:
            raise DapPluginError(
                code=ErrorCode.INVALID_STATE_TRANSITION,
                message=f"Invalid session state transition from {self.state.value} to {target.value}.",
                retryable=False,
                details={
                    "session_id": self.session_id,
                    "current_state": self.state.value,
                    "requested_state": target.value,
                },
            )

        prev = self.state
        self.state = target

        if target == SessionState.STOPPED and prev != SessionState.STOPPED:
            self.stop_generation += 1
            self.current_snapshot_id = f"snap_{self.session_id[-8:]}_{self.stop_generation}"
        elif prev == SessionState.STOPPED and target != SessionState.STOPPED:
            if self.current_snapshot_id:
                self.stale_snapshot_ids.add(self.current_snapshot_id)
            self.stale_frame_ids.update(self.current_frame_ids)
            self.current_frame_ids.clear()
            self.current_snapshot_id = None

        return self.state

    def register_active_frames(self, frame_ids: list[int]) -> None:
        """Record valid frame IDs for the current stop generation."""
        self.current_frame_ids = set(frame_ids)

    def require_stopped(self) -> None:
        """Ensure session is currently STOPPED before inspection or stepping."""
        if self.state != SessionState.STOPPED:
            raise DapPluginError(
                code=ErrorCode.SESSION_NOT_STOPPED,
                message=f"Operation requires session '{self.session_id}' to be STOPPED (current state: {self.state.value}).",
                retryable=False,
                details={"session_id": self.session_id, "state": self.state.value},
            )

    def validate_references(
        self,
        snapshot_id: str | None = None,
        frame_id: int | None = None,
    ) -> None:
        """Verify that snapshot_id and frame_id are not stale from an earlier stop generation."""
        self.require_stopped()

        if snapshot_id is not None and snapshot_id != self.current_snapshot_id:
            raise DapPluginError(
                code=ErrorCode.STALE_SNAPSHOT,
                message=(
                    f"Snapshot reference '{snapshot_id}' is stale. "
                    f"Current stop generation is '{self.current_snapshot_id}'."
                ),
                retryable=False,
                details={
                    "session_id": self.session_id,
                    "requested_snapshot_id": snapshot_id,
                    "current_snapshot_id": self.current_snapshot_id,
                },
            )

        if (
            frame_id is not None
            and self.current_frame_ids
            and frame_id not in self.current_frame_ids
            and frame_id in self.stale_frame_ids
        ):
            raise DapPluginError(
                code=ErrorCode.STALE_SNAPSHOT,
                message=(
                    f"Frame reference frame_id={frame_id} belongs to a previous stop generation "
                    f"and is invalid after continue/step (current snapshot_id='{self.current_snapshot_id}')."
                ),
                retryable=False,
                details={
                    "session_id": self.session_id,
                    "requested_frame_id": frame_id,
                    "current_snapshot_id": self.current_snapshot_id,
                    "valid_frame_ids": sorted(self.current_frame_ids),
                },
            )
