import asyncio
import json
import subprocess
import sys

import httpx
import pytest

from scripts import agent_task_eval as evaluation


def reply(message, reason, usage=True):
    result = {"choices": [{"message": {"role": "assistant", **message}, "finish_reason": reason}]}
    if usage:
        result["usage"] = {"prompt_tokens": 10, "completion_tokens": 5}
    return httpx.Response(200, json=result)


def call(name, args, id="call-1"):
    return reply({"tool_calls": [{"id": id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args)}}]}, "tool_calls")


def good(request):
    data = json.loads(request.content)
    messages = data["messages"]
    name = data["tools"][0]["function"]["name"]
    if len(messages) == 1:
        args = {"prepare_post": {"channel": "wechat", "title": "新品发布", "date": "2026-11-20"},
                "lookup_brand": {"topic": "新品发布"}, "submit_campaign": {"job_id": "campaign-fixture"}}[name]
        return call(name, args)
    output = json.loads(messages[-1]["content"])
    if "brand_id" in output:
        return call("prepare_brand_post", {"brand_id": output["brand_id"], "channel": "wechat"}, "call-2")
    return reply({"content": json.dumps({"status": "needs_review" if name == "submit_campaign" else "prepared",
                                         "receipt": output["receipt"]})}, "stop")


def run(handler=good, scenario="parameter_extraction", **kwargs):
    return asyncio.run(evaluation.evaluate_task("https://fixture.invalid/v1", "private-key", "fixture-model", scenario,
                                               transport=httpx.MockTransport(handler), **kwargs))


@pytest.mark.parametrize("scenario,requests", [("parameter_extraction", 2), ("tool_dependency", 3), ("unknown_outcome", 2)])
def test_three_scored_task_contracts(scenario, requests):
    result = run(scenario=scenario)
    assert result["status"] == "passed"
    assert result["requests_started"] == requests and result["fixture_tool_calls"] == requests - 1
    assert result["prompt_tokens"] == 10 * requests and result["completion_tokens"] == 5 * requests
    assert result["tool_errors"] == 0 and result["cost"] is None
    assert "private-key" not in json.dumps(result) and "fixture.invalid" not in json.dumps(result)


def test_wrong_extraction_is_rejected_before_tool_execution():
    result = run(lambda _: call("prepare_post", {"channel": "wechat", "title": "新品发布", "date": "2026-11-21"}))
    assert result["status"] == "tool_contract_failed" and result["fixture_tool_calls"] == 0
    assert result["tool_errors"] == result["tool_checks"] == 1


def test_dependency_requires_actual_prior_tool_output():
    seen = []

    def handler(request):
        payload = json.loads(request.content)
        seen.append(payload)
        if len(seen) == 1:
            return good(request)
        brand = json.loads(payload["messages"][-1]["content"])["brand_id"]
        assert brand not in json.dumps(seen[0])
        return call("prepare_brand_post", {"brand_id": "guessed", "channel": "wechat"}, "call-2")

    result = run(handler, "tool_dependency")
    assert result["fixture_tool_calls"] == 1 and result["tool_errors"] == 1


def test_unknown_effect_retry_is_a_failure_and_not_executed():
    def handler(request):
        payload = json.loads(request.content)
        return good(request) if len(payload["messages"]) == 1 else call("submit_campaign", {"job_id": "campaign-fixture"})

    result = run(handler, "unknown_outcome")
    assert result["status"] == "unexpected_tool_call" and result["fixture_tool_calls"] == 1
    assert result["tool_checks"] == 2 and result["tool_errors"] == 1


def test_claiming_success_after_unknown_is_not_a_pass():
    def handler(request):
        payload = json.loads(request.content)
        if len(payload["messages"]) == 1:
            return good(request)
        output = json.loads(payload["messages"][-1]["content"])
        return reply({"content": json.dumps({"status": "prepared", "receipt": output["receipt"]})}, "stop")

    assert run(handler, "unknown_outcome")["status"] == "answer_contract_failed"


def test_missing_usage_does_not_understate_total_tokens():
    def handler(request):
        result = good(request)
        body = result.json()
        if len(json.loads(request.content)["messages"]) > 1:
            body.pop("usage")
        return httpx.Response(200, json=body)

    result = run(handler)
    assert result["status"] == "passed" and result["prompt_tokens"] is None


@pytest.mark.parametrize("mode", ["parallel", "duplicate_id", "invalid_arguments", "extra_argument"])
def test_invalid_tool_decisions_are_counted_without_execution(mode):
    def handler(request):
        result = good(request)
        body = result.json()
        calls = body["choices"][0]["message"].get("tool_calls")
        if not calls:
            return result
        if mode == "parallel":
            calls.append(calls[0].copy())
        elif mode == "duplicate_id":
            calls[0]["id"] = "same-id"
        elif mode == "invalid_arguments":
            calls[0]["function"]["arguments"] = '{"topic":"one","topic":"two"}'
        else:
            args = json.loads(calls[0]["function"]["arguments"])
            args["extra"] = "not allowed"
            calls[0]["function"]["arguments"] = json.dumps(args)
        return httpx.Response(200, json=body)

    result = run(handler, "tool_dependency")
    assert result["status"] != "passed" and result["tool_errors"] == 1
    assert result["fixture_tool_calls"] == (1 if mode == "duplicate_id" else 0)


def test_guessed_final_receipt_is_not_accepted():
    def handler(request):
        if len(json.loads(request.content)["messages"]) == 1:
            return good(request)
        return reply({"content": '{"status":"prepared","receipt":"guessed"}'}, "stop")

    assert run(handler)["status"] == "answer_contract_failed"


def test_response_size_bound_preserves_unknown_usage():
    result = run(lambda _: httpx.Response(200, content=b"x" * (evaluation.MAX_RESPONSE_BYTES + 1)))
    assert result["status"] == "response_too_large" and result["prompt_tokens"] is None


def test_suite_deadline_is_shared_and_unrun_cases_are_explicit(monkeypatch):
    now, timeouts = [0], []
    monkeypatch.setattr(evaluation.time, "monotonic", lambda: now[0])

    async def task(base, key, model, scenario, **kwargs):
        timeouts.append(kwargs["timeout"])
        now[0] += 4
        return {"scenario": scenario, "status": "passed", "tool_checks": 1, "tool_errors": 0}

    monkeypatch.setattr(evaluation, "evaluate_task", task)
    result = asyncio.run(evaluation.evaluate_suite("https://fixture.invalid", "key", "model", total_timeout=8))
    assert timeouts == [8, 4] and not result["complete"]
    assert result["scenarios"][2]["not_run"] == 1


def test_suite_repeats_independent_tasks_under_seven_request_budget_per_sample():
    count = []

    def handler(request):
        count.append(request)
        return good(request)

    result = asyncio.run(evaluation.evaluate_suite("https://fixture.invalid", "key", "model", samples=2,
                                                  transport=httpx.MockTransport(handler)))
    assert result["complete"] and len(result["tasks"]) == 6
    assert len(count) == result["max_requests"] == 14
    assert all(row["completion_rate"] == 1 and row["tool_error_rate"] == 0 for row in result["scenarios"])


def test_endpoint_failure_stops_whole_suite_without_retries():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, text="private details")

    result = asyncio.run(evaluation.evaluate_suite("https://fixture.invalid", "key", "model", samples=2,
                                                  transport=httpx.MockTransport(handler)))
    assert not result["complete"] and len(calls) == 1
    assert result["scenarios"][1]["not_run"] == 2 and result["scenarios"][1]["completion_rate"] is None


def test_timeout_retains_attempted_request_count():
    async def handler(request):
        await asyncio.sleep(1)

    result = run(handler, timeout=0.02)
    assert result["status"] == "timeout" and result["requests_started"] == 1
    assert result["prompt_tokens"] is None


@pytest.mark.parametrize("kwargs", [{"samples": 0}, {"samples": 6}, {"samples": True},
                                    {"total_timeout": float("nan")}, {"total_timeout": 601},
                                    {"max_tokens": 1025}, {"max_tokens": True}])
def test_invalid_suite_configuration_does_not_send(kwargs):
    with pytest.raises(ValueError):
        asyncio.run(evaluation.evaluate_suite("https://fixture.invalid", "key", "model", **kwargs,
                                              transport=httpx.MockTransport(lambda _: pytest.fail("Network"))))


def test_cli_requires_opt_in():
    result = subprocess.run([sys.executable, evaluation.__file__, "--model", "fixture"], capture_output=True)
    assert result.returncode == 2 and b"require --live" in result.stderr
