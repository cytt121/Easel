"""A queued/rejected OpenClaw turn must not replace its session owner's snapshot."""
import asyncio

import pytest

import web.app as web


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(web, "SESSIONS_DIR", tmp_path / "sessions")
    monkeypatch.setattr(web, "SHARED_RAW_STREAM", tmp_path / "raw.jsonl")
    monkeypatch.setattr(web, "_session_locks", {})
    monkeypatch.setattr(web, "_BG_TASKS", set())
    monkeypatch.setattr(web, "_direct_api_enabled", lambda: False)
    monkeypatch.setattr(web, "_chat_message", lambda req: req.message)
    monkeypatch.setattr(web, "_heal_openclaw_session", lambda sk: None)
    monkeypatch.setattr(web, "openclaw_base_cmd", lambda: ["unused-openclaw"])
    monkeypatch.setattr(web, "_agent_thinking_level", lambda: "off")
    monkeypatch.setattr(web, "_proxy_env", dict)
    monkeypatch.setattr(web, "question_bridge_supported", lambda: False)
    monkeypatch.setattr(web, "_resolve_transport", lambda sk: "cli")

    class Lock:
        allow = True
        held = False
        releases = 0

        def __init__(self, sk):
            pass

        def acquire(self, timeout):
            Lock.held = Lock.allow
            return Lock.allow

        def release(self):
            Lock.held = False
            Lock.releases += 1

    monkeypatch.setattr(web, "_CrossProcLock", Lock)

    def spawn_failure(*args, **kwargs):
        raise FileNotFoundError("simulated missing CLI")

    monkeypatch.setattr(web.subprocess, "Popen", spawn_failure)
    return Lock


async def collect(response):
    async def drain():
        return [event async for event in response.body_iterator]
    return await asyncio.wait_for(drain(), timeout=2)


@pytest.mark.parametrize("owner_status", ["running", "done"])
def test_queued_turn_preserves_owner_until_lock_acquired(sandbox, owner_status):
    async def scenario():
        lock = web._session_lock("same")
        await lock.acquire()
        web._save_turn("web:same", owner_status, "owner result", {"turn_id": "owner"})
        original = web._turn_file("web:same").read_bytes()
        response = await web.api_chat_stream(web.ChatRequest(
            message="queued", sessionId="same", turnId="queued"))
        try:
            # Wait for the actual queue notification instead of a timing sleep.
            while True:
                event = await asyncio.wait_for(anext(response.body_iterator), timeout=2)
                if "排队" in event.get("data", ""):
                    break
            assert web._turn_file("web:same").read_bytes() == original
            assert (await web.api_chat_last("same", turn_id="owner"))["text"] == "owner result"
            assert (await web.api_chat_last("same", turn_id="queued"))["status"] == "stale"
        finally:
            lock.release()
            await collect(response)
        snapshot = await web.api_chat_last("same", turn_id="queued")
        assert snapshot["stop_reason"] == "spawn_failed"
        assert not lock.locked() and not sandbox.held

    asyncio.run(scenario())


def test_rejected_turn_keeps_owner_and_reports_error_in_its_own_log(sandbox):
    sandbox.allow = False

    async def scenario():
        web._save_turn("web:busy", "running", "owner progress", {"turn_id": "owner"})
        original = web._turn_file("web:busy").read_bytes()
        response = await web.api_chat_stream(web.ChatRequest(
            message="rejected", sessionId="busy", turnId="rejected"))
        events = await collect(response)
        assert web._turn_file("web:busy").read_bytes() == original
        assert any(event["event"] == "error" for event in events)
        replay = web._read_job_events("rejected")
        assert any(event["event"] == "error" for event in replay)
        assert replay[-1]["event"] == "done"
        assert not web._session_lock("busy").locked()
        assert sandbox.releases == 1

    asyncio.run(scenario())


def test_snapshot_writes_remain_inside_both_locks(sandbox, monkeypatch):
    writes = []
    original = web._save_turn

    def save(sk, status, *args, **kwargs):
        writes.append((status, web._session_lock("owned").locked(), sandbox.held))
        return original(sk, status, *args, **kwargs)

    monkeypatch.setattr(web, "_save_turn", save)

    async def scenario():
        response = await web.api_chat_stream(web.ChatRequest(
            message="test", sessionId="owned", turnId="owned-turn"))
        await collect(response)

    asyncio.run(scenario())
    assert writes == [("running", True, True), ("done", True, True)]
    assert not sandbox.held
