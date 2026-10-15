import asyncio
import json
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

from scripts import model_probe_batch as batch


def response(message, reason="stop", usage=True):
    body = {"choices": [{"message": {"role": "assistant", **message}, "finish_reason": reason}]}
    if usage:
        body["usage"] = {"prompt_tokens": 10, "completion_tokens": 5}
    return httpx.Response(200, json=body)


def tool_response():
    return response({"tool_calls": [{"id": "fixture-call", "type": "function", "function": {
        "name": "lookup_receipt", "arguments": '{"ticket":"probe"}'}}]}, "tool_calls")


def run(handler, **kwargs):
    return asyncio.run(batch.evaluate("https://fixture.invalid/v1", "secret-key", "private-model",
                                     transport=httpx.MockTransport(handler), **kwargs))


def test_independent_samples_bound_requests_and_sanitize_report():
    payloads = []
    receipts = []

    def handler(request):
        payload = json.loads(request.content)
        payloads.append(payload)
        if payload["tool_choice"] == "auto":
            assert len(payload["messages"]) == 1
            return tool_response()
        receipts.append(payload["messages"][-1]["content"])
        return response({"content": receipts[-1]})

    report = run(handler, samples=3, max_tokens=123)
    assert report["complete"] and report["passed"] == 3
    assert report["requests_started"] == len(payloads) == report["max_requests"] == 6
    assert len(set(receipts)) == 3
    assert all(payload["max_tokens"] == 123 for payload in payloads)
    assert report["prompt_tokens"] == 60 and report["completion_tokens"] == 30
    assert report["pass_rate_attempted"] == 1 and report["cost"] is None
    encoded = json.dumps(report)
    assert not any(secret in encoded for secret in ["secret-key", "private-model", "fixture.invalid", *receipts])


def test_contract_failure_counts_as_sample_without_retry():
    calls = []

    def handler(request):
        calls.append(request)
        return response({"content": "guessed answer"})

    report = run(handler, samples=4)
    assert len(calls) == report["requests_started"] == 4
    assert report["status_counts"] == {"tool_contract_failed": 4}
    assert report["complete"] and report["pass_rate_attempted"] == 0


@pytest.mark.parametrize("status", [401, 429, 500, 302])
def test_endpoint_errors_stop_batch_without_retries(status):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="private provider error", headers={"location": "https://other.invalid"})

    report = run(handler, samples=20)
    assert len(calls) == 1
    assert not report["complete"] and report["samples_not_run"] == 19
    assert report["stop_reason"] == "http_error"
    assert report["prompt_tokens"] is None and "private" not in json.dumps(report)


def test_real_async_timeout_stops_without_losing_started_request_accounting():
    async def handler(request):
        await asyncio.sleep(1)
        raise AssertionError("Timeout should cancel this request")

    report = run(handler, samples=3, total_timeout=0.02)
    assert report["requests_started"] == 1 and report["responses_received"] == 0
    assert report["stop_reason"] == "timeout" and report["samples_not_run"] == 2
    assert report["prompt_tokens"] is None


def test_partial_usage_is_unknown_across_whole_batch():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return response({"content": "no tool"}, usage=calls != 2)

    report = run(handler, samples=3)
    assert report["requests_started"] == 3
    assert report["prompt_tokens"] is None and report["completion_tokens"] is None


def test_transport_failure_after_success_keeps_counts_but_not_partial_usage():
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return tool_response()
        if calls == 2:
            return response({"content": json.loads(request.content)["messages"][-1]["content"]})
        raise httpx.ConnectError("secret endpoint detail", request=request)

    report = run(handler, samples=4)
    assert calls == report["requests_started"] == 3
    assert report["responses_received"] == 2 and report["fixture_tool_calls"] == 1
    assert report["pass_rate_attempted"] == 0.5 and report["samples_not_run"] == 2
    assert report["stop_reason"] == "transport_error" and report["prompt_tokens"] is None
    assert "secret" not in json.dumps(report)


def test_latency_population_includes_failures_and_p95_uses_nearest_rank(monkeypatch):
    durations = iter(range(1, 21))

    async def fake_probe(*args, **kwargs):
        duration = next(durations)
        return {"status": "passed" if duration % 2 else "tool_contract_failed",
                "elapsed_seconds": duration, "requests_started": 1,
                "responses_received": 1, "fixture_tool_calls": 0,
                "prompt_tokens": 0, "completion_tokens": 0}

    monkeypatch.setattr(batch, "probe", fake_probe)
    report = run(None, samples=20)
    assert report["latency_seconds"]["median"] == 10.5
    assert report["latency_seconds"]["p95_nearest_rank"] == 19
    assert report["passed"] == 10 and report["prompt_tokens"] == 0


def test_remaining_time_is_shared_and_unstarted_samples_are_not_failures(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(batch.time, "monotonic", lambda: now[0])
    timeouts = []

    async def fake_probe(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        now[0] += 4
        return {"status": "passed", "elapsed_seconds": 4, "requests_started": 2,
                "responses_received": 2, "fixture_tool_calls": 1,
                "prompt_tokens": 10, "completion_tokens": 5}

    monkeypatch.setattr(batch, "probe", fake_probe)
    report = run(None, samples=4, total_timeout=8)
    assert timeouts == [8, 4]
    assert report["samples_attempted"] == 2 and report["samples_not_run"] == 2
    assert report["pass_rate_attempted"] == 1 and not report["complete"]
    assert report["stop_reason"] == "total_timeout"
    assert report["latency_seconds"] == {"population": "all_attempted_samples", "count": 2,
                                         "median": 4, "p95_nearest_rank": 4}


@pytest.mark.parametrize("options", [
    {"samples": 0}, {"samples": 21}, {"samples": True}, {"samples": 1.5},
    {"max_tokens": 0}, {"max_tokens": 1025}, {"max_tokens": True},
    {"total_timeout": 0}, {"total_timeout": 601}, {"total_timeout": float("nan")},
    {"total_timeout": float("inf")}, {"total_timeout": True},
])
def test_invalid_limits_never_send_requests(options):
    def handler(request):
        pytest.fail("Invalid configuration sent a request")

    with pytest.raises(ValueError):
        run(handler, **options)


def test_cli_requires_explicit_live_opt_in():
    script = Path(batch.__file__)
    result = subprocess.run([sys.executable, str(script), "--model", "fixture"],
                            capture_output=True, text=True)
    assert result.returncode == 2 and "require --live" in result.stderr


@pytest.mark.parametrize("complete,passed,code", [(True, 3, 0), (True, 2, 1), (False, 2, 1)])
def test_cli_exit_status_distinguishes_incomplete_and_failed(monkeypatch, capsys, complete, passed, code):
    async def fake_evaluate(*args, **kwargs):
        return {"complete": complete, "passed": passed, "samples_planned": 3}

    monkeypatch.setattr(batch, "evaluate", fake_evaluate)
    monkeypatch.setenv("EASEL_PROBE_BASE_URL", "https://fixture.invalid")
    monkeypatch.setenv("EASEL_PROBE_API_KEY", "secret-key")
    monkeypatch.setattr(sys, "argv", ["batch", "--live", "--model", "fixture"])
    assert batch.main() == code
    assert json.loads(capsys.readouterr().out)["complete"] == complete
