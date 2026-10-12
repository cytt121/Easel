"""A queued/rejected OpenClaw turn must not replace its session owner's snapshot."""
import asyncio
import subprocess
import sys

import pytest

import web.app as web


REAL_POPEN = subprocess.Popen
REAL_CROSS_PROC_LOCK = web._CrossProcLock
REAL_RESOLVE_TRANSPORT = web._resolve_transport


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


def test_invalid_transport_pin_finishes_turn_and_releases_ownership(sandbox, monkeypatch, tmp_path):
    monkeypatch.setattr(web, "_resolve_transport", REAL_RESOLVE_TRANSPORT)
    monkeypatch.setattr(web, "OPENCLAW_SESSIONS_DIR", tmp_path / "openclaw-sessions")
    web.SESSIONS_DIR.mkdir(parents=True)
    pin = web._transport_pin_file("broken-pin")
    pin.write_bytes(b"\xff\xfe")

    async def scenario():
        response = await web.api_chat_stream(web.ChatRequest(
            message="test", sessionId="broken-pin", turnId="broken-pin-turn"))
        events = await collect(response)
        assert any(event["event"] == "error" for event in events)
        assert events[-1]["event"] == "done"
        snapshot = await web.api_chat_last("broken-pin", turn_id="broken-pin-turn")
        assert snapshot["status"] == "done"
        assert snapshot["stop_reason"] == "spawn_failed"
        assert not web._session_lock("broken-pin").locked()
        assert not sandbox.held
        # A damaged pin must not silently move an established HTTP session to
        # the CLI's different transcript. Leave it intact and report failure.
        assert pin.read_bytes() == b"\xff\xfe"

    asyncio.run(scenario())


def test_other_process_owner_survives_rejection_and_next_turn_can_start(sandbox, monkeypatch):
    """Exercise real OS locks, not only the fake ownership boundary above."""
    monkeypatch.setattr(web, "_CrossProcLock", REAL_CROSS_PROC_LOCK)
    monkeypatch.setattr(web, "TIMEOUT_CHAT", 0)
    script = """
import sys
from pathlib import Path
import web.app as web
web.SESSIONS_DIR = Path(sys.argv[1])
lock = web._CrossProcLock('cross-process')
try:
    assert lock.acquire(0), 'child failed to acquire session lock'
    print('locked', flush=True)
    sys.stdin.readline()
finally:
    lock.release()
"""
    child = REAL_POPEN(
        [sys.executable, "-u", "-c", script, str(web.SESSIONS_DIR)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )

    async def scenario():
        try:
            ready = await asyncio.wait_for(asyncio.to_thread(child.stdout.readline), timeout=10)
            assert ready.strip() == "locked"
            web._save_turn("web:cross-process", "running", "owner result", {"turn_id": "owner"})
            original = web._turn_file("web:cross-process").read_bytes()
            response = await web.api_chat_stream(web.ChatRequest(
                message="blocked", sessionId="cross-process", turnId="blocked"))
            events = await collect(response)
            assert any(event["event"] == "error" for event in events)
            assert web._turn_file("web:cross-process").read_bytes() == original
            assert child.poll() is None
            assert not web._session_lock("cross-process").locked()

            child.stdin.write("release\n")
            child.stdin.flush()
            assert await asyncio.to_thread(child.wait, timeout=5) == 0
            # With the real owner gone, the next turn must acquire the same
            # session and reach the simulated CLI startup failure normally.
            response = await web.api_chat_stream(web.ChatRequest(
                message="next", sessionId="cross-process", turnId="next"))
            await collect(response)
            snapshot = await web.api_chat_last("cross-process", turn_id="next")
            assert snapshot["stop_reason"] == "spawn_failed"
            assert not web._session_lock("cross-process").locked()
        finally:
            if child.poll() is None:
                child.kill()
            await asyncio.to_thread(child.wait, timeout=5)
            for pipe in (child.stdin, child.stdout, child.stderr):
                pipe.close()

    asyncio.run(scenario())
