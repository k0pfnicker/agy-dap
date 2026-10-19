"""Unit tests for session lifecycle transitions, stale references, concurrency, and idempotent cleanup."""

from __future__ import annotations

import asyncio

import pytest

from agy_dap.config import PluginConfig
from agy_dap.errors import DapPluginError, ErrorCode
from agy_dap.models import BreakpointSpec
from agy_dap.sessions.lifecycle import LifecycleTracker, SessionState
from agy_dap.sessions.manager import SessionManager
from tests.conftest import FakeSubprocessAdapter


def test_lifecycle_invalid_transitions_rejected() -> None:
    """Verify invalid state transitions raise INVALID_STATE_TRANSITION."""
    tracker = LifecycleTracker("sess_test1234")
    assert tracker.state == SessionState.CREATED

    with pytest.raises(DapPluginError) as exc_info:
        tracker.transition_to(SessionState.STOPPED)
    assert exc_info.value.code == ErrorCode.INVALID_STATE_TRANSITION

    tracker.transition_to(SessionState.STARTING)
    tracker.transition_to(SessionState.CONFIGURING)
    tracker.transition_to(SessionState.RUNNING)
    tracker.transition_to(SessionState.STOPPED)
    snap_1 = tracker.current_snapshot_id
    assert snap_1 is not None

    tracker.register_active_frames([101])
    tracker.transition_to(SessionState.RUNNING)

    with pytest.raises(DapPluginError) as exc_info2:
        tracker.require_stopped()
    assert exc_info2.value.code == ErrorCode.SESSION_NOT_STOPPED

    tracker.transition_to(SessionState.STOPPED)
    snap_2 = tracker.current_snapshot_id
    assert snap_2 != snap_1
    tracker.register_active_frames([102])

    # Old snapshot_id or old frame_id must raise STALE_SNAPSHOT
    with pytest.raises(DapPluginError) as stale_snap_exc:
        tracker.validate_references(snapshot_id=snap_1)
    assert stale_snap_exc.value.code == ErrorCode.STALE_SNAPSHOT

    with pytest.raises(DapPluginError) as stale_frame_exc:
        tracker.validate_references(frame_id=101)
    assert stale_frame_exc.value.code == ErrorCode.STALE_SNAPSHOT


@pytest.mark.asyncio
async def test_stale_snapshot_and_idempotent_cleanup_in_session(test_config: PluginConfig) -> None:
    """Verify stepping invalidates previous snapshot_id and frame_id, and cleanup is idempotent."""
    mgr = SessionManager(
        test_config,
        adapter_factory=lambda cfg: FakeSubprocessAdapter(cfg, mode="normal"),
    )
    sess = await mgr.create_session("pytest", "tests/test_checkout.py::test_discount")
    snap1 = await sess.start_and_wait_first_stop(
        breakpoints=[BreakpointSpec(file="src/checkout.py", line=38)],
        timeout_seconds=5.0,
    )
    old_snap_id = snap1["snapshot_id"]
    old_frame_id = snap1["frame_id"]

    # Step to next stop generation
    snap2 = await sess.continue_or_step(action="next", timeout_seconds=5.0)
    assert snap2["snapshot_id"] != old_snap_id
    assert snap2["frame_id"] != old_frame_id

    # Querying with old snapshot_id must fail with STALE_SNAPSHOT
    with pytest.raises(DapPluginError) as exc_info:
        await sess.get_snapshot(snapshot_id=old_snap_id)
    assert exc_info.value.code == ErrorCode.STALE_SNAPSHOT

    # Querying with old frame_id must fail with STALE_SNAPSHOT
    with pytest.raises(DapPluginError) as exc_info2:
        await sess.get_snapshot(frame_id=old_frame_id)
    assert exc_info2.value.code == ErrorCode.STALE_SNAPSHOT

    # Idempotent stop calls
    c1 = await mgr.stop_session(sess.session_id)
    c2 = await mgr.stop_session(sess.session_id)
    assert c1["state"] == "TERMINATED"
    assert c2["state"] == "TERMINATED"
    assert c2.get("already_terminated") is True


@pytest.mark.asyncio
async def test_concurrent_session_limit_and_idle_timeout(test_config: PluginConfig) -> None:
    """Verify single active session limit and automatic idle session cleanup."""
    test_config.timeouts.idleSessionTimeoutSec = 0.25
    mgr = SessionManager(
        test_config,
        adapter_factory=lambda cfg: FakeSubprocessAdapter(cfg, mode="normal"),
    )
    sess = await mgr.create_session("pytest", "tests/test_checkout.py::test_discount")
    await sess.start_and_wait_first_stop(
        breakpoints=[BreakpointSpec(file="src/checkout.py", line=38)],
        timeout_seconds=5.0,
    )
    sess.start_idle_monitor()

    # Second concurrent session must be rejected while first is active
    with pytest.raises(DapPluginError) as exc_info:
        await mgr.create_session("pytest", "tests/test_checkout.py::test_discount")
    assert exc_info.value.code == ErrorCode.CONCURRENT_SESSION_LIMIT

    # Wait for idle timeout watchdog to reap first session automatically
    assert sess._idle_task is not None
    await asyncio.wait_for(sess._idle_task, timeout=3.0)
    assert sess.state == SessionState.TERMINATED
    assert mgr.active_session_ids == []


@pytest.mark.asyncio
async def test_concurrent_operations_on_same_session_are_serialized(test_config: PluginConfig) -> None:
    """Verify concurrent get_snapshot calls on the same session are serialized safely by operation_lock."""
    mgr = SessionManager(
        test_config,
        adapter_factory=lambda cfg: FakeSubprocessAdapter(cfg, mode="normal"),
    )
    sess = await mgr.create_session("pytest", "tests/test_checkout.py::test_discount")
    await sess.start_and_wait_first_stop(
        breakpoints=[BreakpointSpec(file="src/checkout.py", line=38)],
        timeout_seconds=5.0,
    )
    try:
        res1, res2, res3 = await asyncio.gather(
            sess.get_snapshot(locals_filter=["subtotal"]),
            sess.get_snapshot(locals_filter=["discount"]),
            sess.get_snapshot(locals_filter=["tax"]),
        )
        assert res1["locals"]["subtotal"]["value"] == 100.0
        assert res2["locals"]["discount"]["value"] == 15.0
        assert res3["locals"]["tax"]["value"] == 0
    finally:
        await sess.cleanup()
