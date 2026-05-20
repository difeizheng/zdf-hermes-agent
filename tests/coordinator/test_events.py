"""Tests for the SSE event broadcaster."""

from __future__ import annotations

import asyncio

import pytest

from coordinator.events import TaskEventBroadcaster
from coordinator.models import TaskEvent


@pytest.fixture
def broadcaster() -> TaskEventBroadcaster:
    return TaskEventBroadcaster()


@pytest.mark.asyncio
async def test_subscribe_and_publish(broadcaster: TaskEventBroadcaster) -> None:
    last_id, queue = await broadcaster.subscribe()
    await broadcaster.publish(TaskEvent.CREATED, task_id="t1", task_type="design")
    payload = await asyncio.wait_for(queue.get(), timeout=1.0)
    assert payload["type"] == "created"
    assert payload["task_id"] == "t1"


@pytest.mark.asyncio
async def test_filter_by_task_type(broadcaster: TaskEventBroadcaster) -> None:
    _, q_design = await broadcaster.subscribe(task_type="design")
    _, q_dev = await broadcaster.subscribe(task_type="dev")

    await broadcaster.publish(TaskEvent.CREATED, task_id="t1", task_type="design")

    payload = await asyncio.wait_for(q_design.get(), timeout=1.0)
    assert payload["task_id"] == "t1"

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q_dev.get(), timeout=0.2)


@pytest.mark.asyncio
async def test_filter_by_task_id(broadcaster: TaskEventBroadcaster) -> None:
    _, q1 = await broadcaster.subscribe(task_id="t1")
    _, q2 = await broadcaster.subscribe(task_id="t2")

    await broadcaster.publish(TaskEvent.CREATED, task_id="t1", task_type="design")

    payload = await asyncio.wait_for(q1.get(), timeout=1.0)
    assert payload["task_id"] == "t1"

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(q2.get(), timeout=0.2)


@pytest.mark.asyncio
async def test_unsubscribe(broadcaster: TaskEventBroadcaster) -> None:
    _, queue = await broadcaster.subscribe()
    await broadcaster.unsubscribe(queue)

    await broadcaster.publish(TaskEvent.CREATED, task_id="t1", task_type="design")

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(queue.get(), timeout=0.2)


@pytest.mark.asyncio
async def test_event_id_increments(broadcaster: TaskEventBroadcaster) -> None:
    _, queue = await broadcaster.subscribe()

    await broadcaster.publish(TaskEvent.CREATED, task_id="t1", task_type="design")
    e1 = await asyncio.wait_for(queue.get(), timeout=1.0)

    await broadcaster.publish(TaskEvent.STARTED, task_id="t1", task_type="design")
    e2 = await asyncio.wait_for(queue.get(), timeout=1.0)

    assert e2["event_id"] == e1["event_id"] + 1


@pytest.mark.asyncio
async def test_multiple_subscribers(broadcaster: TaskEventBroadcaster) -> None:
    _, q1 = await broadcaster.subscribe()
    _, q2 = await broadcaster.subscribe()

    await broadcaster.publish(TaskEvent.CREATED, task_id="t1", task_type="design")

    p1 = await asyncio.wait_for(q1.get(), timeout=1.0)
    p2 = await asyncio.wait_for(q2.get(), timeout=1.0)

    assert p1["task_id"] == "t1"
    assert p2["task_id"] == "t1"


@pytest.mark.asyncio
async def test_sse_stream_format(broadcaster: TaskEventBroadcaster) -> None:
    """Verify sse_stream yields correctly formatted SSE output."""
    sse_output = ""

    async def _collect_one_message():
        nonlocal sse_output
        async for chunk in broadcaster.sse_stream():
            sse_output = chunk  # sse_stream yields one complete SSE message per iteration
            break

    consumer = asyncio.create_task(_collect_one_message())
    await asyncio.sleep(0.1)

    await broadcaster.publish(TaskEvent.CREATED, task_id="t1", task_type="design", data={"title": "Test"})

    await asyncio.wait_for(consumer, timeout=3.0)

    assert len(sse_output) > 0, "No SSE output received"
    assert "id:" in sse_output
    assert "event:" in sse_output
    assert "data:" in sse_output


@pytest.mark.asyncio
async def test_no_filter_receives_all(broadcaster: TaskEventBroadcaster) -> None:
    _, queue = await broadcaster.subscribe()

    await broadcaster.publish(TaskEvent.CREATED, task_id="t1", task_type="design")
    await broadcaster.publish(TaskEvent.COMPLETED, task_id="t2", task_type="dev")

    e1 = await asyncio.wait_for(queue.get(), timeout=1.0)
    e2 = await asyncio.wait_for(queue.get(), timeout=1.0)

    assert e1["task_id"] == "t1"
    assert e2["task_id"] == "t2"
