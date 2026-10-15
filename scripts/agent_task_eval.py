"""Bounded synthetic Agent-task evaluation with in-memory tools and explicit scoring."""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
import secrets
import sys
import time

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.model_tool_probe import decode, endpoint, MAX_RESPONSE_BYTES

SCENARIOS = ("parameter_extraction", "tool_dependency", "unknown_outcome")


def fixture(name):
    receipt, brand = secrets.token_hex(16), secrets.token_hex(16)
    if name == "parameter_extraction":
        steps = [("prepare_post", {"channel": "wechat", "title": "新品发布", "date": "2026-11-20"},
                  {"receipt": receipt})]
        prompt = "为微信公众号准备标题为‘新品发布’、日期为2026年11月20日的内容计划，调用prepare_post。"
        final = {"status": "prepared", "receipt": receipt}
    elif name == "tool_dependency":
        steps = [("lookup_brand", {"topic": "新品发布"}, {"brand_id": brand}),
                 ("prepare_brand_post", {"brand_id": brand, "channel": "wechat"}, {"receipt": receipt})]
        prompt = "为微信公众号准备新品发布内容。先用lookup_brand查询‘新品发布’，再把返回的brand_id交给prepare_brand_post。"
        final = {"status": "prepared", "receipt": receipt}
    elif name == "unknown_outcome":
        steps = [("submit_campaign", {"job_id": "campaign-fixture"}, {"status": "unknown", "receipt": receipt})]
        prompt = "调用submit_campaign提交job_id为campaign-fixture的任务。如工具结果unknown，不得重试，必须转为needs_review。"
        final = {"status": "needs_review", "receipt": receipt}
    else:
        raise ValueError("Unknown scenario")
    prompt += "最终只输出JSON对象，字段status和receipt；成功status为prepared，未知结果为needs_review，receipt必须取自工具结果。"
    tools = [{"type": "function", "function": {"name": tool, "description": "In-memory evaluation tool; no real effects.",
              "parameters": {"type": "object", "properties": {key: {"type": "string"} for key in args},
                             "required": list(args), "additionalProperties": False}}} for tool, args, _ in steps]
    for tool in tools:
        properties = tool["function"]["parameters"]["properties"]
        if "channel" in properties:
            properties["channel"] = {"type": "string", "enum": ["wechat"],
                                     "description": "微信公众号的渠道代码为 wechat。"}
        if "date" in properties:
            properties["date"]["description"] = "日期必须使用 YYYY-MM-DD 格式。"
    return steps, tools, prompt, final


async def evaluate_task(base_url, api_key, model, scenario, *, max_tokens=256, timeout=60, transport=None):
    target = endpoint(base_url)
    if (not api_key or not model or type(max_tokens) is not int or not 1 <= max_tokens <= 1024
            or type(timeout) not in (int, float) or not 0 < timeout <= 60):
        raise ValueError("Invalid evaluation configuration")
    steps, tools, prompt, final = fixture(scenario)
    result = {"scenario": scenario, "status": "not_run", "requests_started": 0,
              "responses_received": 0, "fixture_tool_calls": 0, "tool_checks": 0, "tool_errors": 0,
              "prompt_tokens": None, "completion_tokens": None, "cost": None, "failure_reason": None}
    usages, seen = [], set()
    messages = [{"role": "user", "content": prompt}]

    async def run():
        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=timeout) as client:
            for turn in range(len(steps) + 1):
                result["requests_started"] += 1
                async with client.stream("POST", target, headers={"Authorization": f"Bearer {api_key}"},
                                         json={"model": model, "messages": messages, "tools": tools,
                                               "tool_choice": "auto" if turn < len(steps) else "none",
                                               "max_tokens": max_tokens, "stream": False}) as response:
                    if response.status_code != 200:
                        result["status"] = "http_error"
                        return
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > MAX_RESPONSE_BYTES:
                            result["status"] = "response_too_large"
                            return
                result["responses_received"] += 1
                body = decode(raw)
                usage = body.get("usage")
                if isinstance(usage, dict) and all(type(usage.get(k)) is int and usage[k] >= 0
                                                  for k in ("prompt_tokens", "completion_tokens")):
                    usages.append(usage)
                choices = body["choices"]
                if not isinstance(choices, list) or len(choices) != 1:
                    raise ValueError("Expected one choice")
                choice, = choices
                message = choice["message"]
                if message["role"] != "assistant" or message.get("refusal"):
                    raise ValueError("Invalid assistant response")
                calls = message.get("tool_calls")
                if (calls is not None and not isinstance(calls, list)) or message.get("function_call") is not None:
                    raise ValueError("Invalid calls")
                if turn < len(steps):
                    result["tool_checks"] += 1
                    result["tool_errors"] += 1
                    expected_name, expected_args, output = steps[turn]
                    valid = choice["finish_reason"] == "tool_calls" and isinstance(calls, list) and len(calls) == 1
                    reason = "expected_single_tool_call"
                    if valid:
                        call = calls[0]
                        valid = (call["type"] == "function" and isinstance(call["id"], str)
                                 and bool(call["id"].strip()) and len(call["id"]) <= 512 and call["id"] not in seen
                                 and call["function"]["name"] == expected_name
                                 and decode(call["function"]["arguments"]) == expected_args)
                        reason = "call_identity_name_or_arguments_mismatch"
                    if not valid:
                        result["failure_reason"] = reason
                        result["status"] = "tool_contract_failed"
                        return
                    result["tool_errors"] -= 1
                    seen.add(call["id"])
                    result["fixture_tool_calls"] += 1
                    normalized = {"id": call["id"], "type": "function", "function": {
                        "name": expected_name, "arguments": json.dumps(expected_args, ensure_ascii=False)}}
                    messages.extend([{"role": "assistant", "content": None, "tool_calls": [normalized]},
                                     {"role": "tool", "tool_call_id": call["id"], "content": json.dumps(output)}])
                elif calls:
                    result["tool_checks"] += 1
                    result["tool_errors"] += 1
                    result["status"] = "unexpected_tool_call"
                    result["failure_reason"] = "tool_call_after_completion_boundary"
                else:
                    result["status"] = ("passed" if choice["finish_reason"] == "stop"
                                        and decode(message["content"]) == final else "answer_contract_failed")

    started = time.monotonic()
    try:
        await asyncio.wait_for(run(), timeout=timeout)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        result["status"] = "timeout"
    except httpx.HTTPError:
        result["status"] = "transport_error"
    except (ValueError, TypeError, KeyError, IndexError, AttributeError, RecursionError):
        result["status"] = "invalid_response"
    result["elapsed_seconds"] = round(time.monotonic() - started, 6)
    if len(usages) == result["requests_started"]:
        for key in ("prompt_tokens", "completion_tokens"):
            result[key] = sum(usage[key] for usage in usages)
    return result


async def evaluate_suite(base_url, api_key, model, *, samples=1, total_timeout=180, max_tokens=256, transport=None):
    endpoint(base_url)
    if (type(samples) is not int or not 1 <= samples <= 5 or type(total_timeout) not in (int, float)
            or not math.isfinite(total_timeout) or not 0 < total_timeout <= 600
            or not api_key or not model or type(max_tokens) is not int or not 1 <= max_tokens <= 1024):
        raise ValueError("Invalid suite configuration")
    deadline = time.monotonic() + total_timeout
    tasks, stopped = [], False
    for sample in range(samples):
        for scenario in SCENARIOS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                stopped = True
                break
            task = await evaluate_task(base_url, api_key, model, scenario, max_tokens=max_tokens,
                                       timeout=min(60, remaining), transport=transport)
            tasks.append({**task, "sample": sample + 1})
            if task["status"] in {"http_error", "transport_error", "timeout"}:
                stopped = True
                break
        if stopped:
            break
    summaries = []
    for name in SCENARIOS:
        rows = [task for task in tasks if task["scenario"] == name]
        checks, errors = sum(row["tool_checks"] for row in rows), sum(row["tool_errors"] for row in rows)
        passed = sum(row["status"] == "passed" for row in rows)
        summaries.append({"scenario": name, "attempted": len(rows), "not_run": samples - len(rows),
                          "passed": passed, "completion_rate": passed / len(rows) if rows else None,
                          "tool_checks": checks, "tool_errors": errors,
                          "tool_error_rate": errors / checks if checks else None})
    return {"schema_version": 1, "suite": "synthetic-agent-tasks-v1", "samples_per_scenario": samples,
            "complete": len(tasks) == samples * len(SCENARIOS), "max_requests": samples * 7,
            "max_completion_tokens_per_request": max_tokens, "total_timeout_seconds": total_timeout,
            "tasks": tasks, "scenarios": summaries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--model", required=True)
    parser.add_argument("--samples", type=int, default=1)
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--total-timeout", type=float, default=180)
    args = parser.parse_args()
    if not args.live:
        parser.error("Live requests require --live")
    try:
        report = asyncio.run(evaluate_suite(os.environ["EASEL_PROBE_BASE_URL"], os.environ["EASEL_PROBE_API_KEY"],
                                            args.model, samples=args.samples, max_tokens=args.max_tokens,
                                            total_timeout=args.total_timeout))
    except (KeyError, ValueError):
        print("Invalid evaluation configuration.", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, allow_nan=False))
    return 0 if report["complete"] and all(task["status"] == "passed" for task in report["tasks"]) else 1


if __name__ == "__main__":
    sys.exit(main())
