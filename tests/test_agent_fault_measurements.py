"""Measured recovery of an idle native process; no real model or tools."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time

import pytest

from easel.harnesses.acp import ACPAdapter
from easel.harnesses.base import RunRequest
from easel.harnesses.codex import CodexAdapter
from easel.harnesses.rpc import RPC, close_sessions


@pytest.mark.parametrize("runtime,adapter", [("codex", CodexAdapter), ("acp", ACPAdapter)], ids=["codex", "acp"])
def test_idle_process_death_recovery(tmp_path, monkeypatch, record_property, runtime, adapter):
    children = []
    original = RPC.__aenter__
    async def capture(rpc):
        result = await original(rpc)
        children.append(rpc.process)
        return result
    monkeypatch.setattr(RPC, "__aenter__", capture)
    audit = tmp_path / "audit.jsonl"
    fixture = Path(__file__).parent / "fixtures" / "harness_rpc.py"
    request = RunRequest("hello", tmp_path, {**os.environ, "FAKE_HARNESS_MODE": "stable-resume",
                         "FAKE_HARNESS_AUDIT": str(audit)},
                         command=(sys.executable, str(fixture), runtime), session_key="measured", timeout=10)
    identifiers, events = [], []
    async def reject(*args):
        raise AssertionError("Unexpected permission request")
    async def exercise():
        try:
            await adapter().run(request, lambda *event: events.append(event), reject, identifiers.append)
            children[0].kill()
            await children[0].wait()
            started = time.perf_counter()
            await adapter().run(replace(request, native_id=identifiers[0]),
                                lambda *event: events.append(event), reject, identifiers.append)
            return time.perf_counter() - started
        finally:
            await close_sessions()
    elapsed = asyncio.run(exercise())
    messages = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    methods = [message.get("method") for message in messages]
    prompts = methods.count("turn/start" if runtime == "codex" else "session/prompt")
    record_property("easel_fault_metrics", json.dumps({
        "prompt_submissions": prompts, "duplicate_prompt_submissions": max(0, prompts - 2),
        "recovery_latency_seconds": elapsed}))
    assert len(children) == 2 and all(child.returncode is not None for child in children)
    assert identifiers == [identifiers[0]] * 2
    assert methods.count("thread/resume" if runtime == "codex" else "session/load") == 1
    assert prompts == 2
    assert events == [("thinking", "思考"), ("token", "你好")] * 2
