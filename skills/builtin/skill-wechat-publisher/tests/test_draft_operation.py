from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import threading

import pytest
import requests

import draft_operation as operation

PAYLOAD = {"articles": [{"title": "private title", "content": "private body", "thumb_media_id": "cover"}]}


@pytest.fixture
def ledger_path(tmp_path, monkeypatch):
    path = tmp_path / "ledger.sqlite"
    operation.DraftLedger(path, create=True).close()
    monkeypatch.setattr(operation, "get_config", lambda name: {"app_id": "private-app", "account_key": name})
    monkeypatch.setattr(operation, "get_access_token", lambda **kwargs: "private-token")
    return path


def execute(path, *, action="submit", payload=PAYLOAD, identity="task"):
    ledger = operation.DraftLedger(path)
    try:
        return operation.execute(ledger, identity, payload, "account", action=action)
    finally:
        ledger.close()


def response(body, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(body).encode()
    return result


def test_lost_receipt_survives_reopen_and_never_replays(ledger_path, monkeypatch):
    calls = []

    def send(*args, **kwargs):
        calls.append(args)
        raise requests.Timeout("private-token")  # server may already have created the draft

    monkeypatch.setattr(operation.api.requests, "request", send)
    assert execute(ledger_path)["state"] == "unknown"
    assert execute(ledger_path)["request_started"] is False
    assert execute(ledger_path, action="reconcile")["reconciliation"] == "unknown_no_receipt"
    assert len(calls) == 1
    assert b"private" not in ledger_path.read_bytes()


def test_known_receipt_uses_only_read_only_lookup_and_never_resubmits(ledger_path, monkeypatch):
    calls = []

    def send(method, url, **kwargs):
        calls.append(url.split("?")[0])
        assert kwargs["allow_redirects"] is False
        if "/draft/add?" in url:
            return response({"media_id": "receipt"})
        assert kwargs["json"] == {"media_id": "receipt"}
        return response({"news_item": [{"title": "normalized by server"}]})

    monkeypatch.setattr(operation.api.requests, "request", send)
    assert execute(ledger_path)["state"] == "acknowledged"
    assert execute(ledger_path)["request_started"] is False
    result = execute(ledger_path, action="reconcile")
    assert result["reconciliation"] == "receipt_found"
    assert len(calls) == 2 and calls[-1].endswith("/draft/get")
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("body,status", [({}, 200), ({"errcode": 40007}, 200),
                                        ({"news_item": []}, 200), ({"news_item": [{}]}, 200),
                                        ({"news_item": [{"title": "x"}]}, 500)])
def test_absent_or_malformed_remote_result_does_not_reopen_submission(ledger_path, monkeypatch, body, status):
    calls = []

    def send(method, url, **kwargs):
        calls.append(url)
        return response({"media_id": "receipt"}) if "/draft/add?" in url else response(body, status)

    monkeypatch.setattr(operation.api.requests, "request", send)
    execute(ledger_path)
    assert execute(ledger_path, action="reconcile")["reconciliation"] == "unknown"
    assert execute(ledger_path)["request_started"] is False
    assert len(calls) == 2


def test_conflicting_account_or_payload_cannot_access_or_repeat_operation(ledger_path, monkeypatch):
    monkeypatch.setattr(operation.api.requests, "request", lambda *a, **k: response({"media_id": "receipt"}))
    execute(ledger_path)
    with pytest.raises(operation.OperationConflict):
        execute(ledger_path, payload={"articles": [{"title": "changed"}]})
    monkeypatch.setattr(operation, "get_config", lambda name: {"app_id": "another-app", "account_key": name})
    with pytest.raises(operation.OperationConflict):
        execute(ledger_path, action="reconcile")


def test_explicit_rejection_is_durable_and_not_retried(ledger_path, monkeypatch):
    calls = []
    monkeypatch.setattr(operation.api.requests, "request",
                        lambda *a, **k: calls.append(a) or response({"errcode": 40007}))
    assert execute(ledger_path)["state"] == "failed"
    assert execute(ledger_path)["state"] == "failed"
    assert len(calls) == 1


def test_concurrent_submitters_share_one_committed_intent(ledger_path, monkeypatch):
    calls, lock = [], threading.Lock()

    def send(*args, **kwargs):
        with lock:
            calls.append(args)
        raise requests.Timeout("fixture")

    monkeypatch.setattr(operation.api.requests, "request", send)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: execute(ledger_path), range(4)))
    assert len(calls) == 1 and sum(item["request_started"] for item in results) == 1


def test_receipt_persistence_failure_leaves_committed_unknown(ledger_path, monkeypatch):
    ledger = operation.DraftLedger(ledger_path)
    ledger.db.execute("CREATE TRIGGER reject_outcome BEFORE UPDATE ON operations BEGIN SELECT RAISE(ABORT,'fixture'); END")
    ledger.close()
    calls = []
    monkeypatch.setattr(operation.api.requests, "request",
                        lambda *a, **k: calls.append(a) or response({"media_id": "receipt"}))
    with pytest.raises(operation.sqlite3.Error):
        execute(ledger_path)
    assert execute(ledger_path)["state"] == "unknown"
    assert len(calls) == 1


def test_account_change_during_token_fetch_blocks_submission(ledger_path, monkeypatch):
    def token(**kwargs):
        assert kwargs == {"force_refresh": True, "account_name": "account"}
        monkeypatch.setattr(operation, "get_config", lambda name: {"app_id": "different", "account_key": name})
        return "private-token"

    monkeypatch.setattr(operation, "get_access_token", token)
    assert execute(ledger_path)["request_started"] is False


def test_query_timeout_keeps_receipt_and_status_never_fetches_token(ledger_path, monkeypatch):
    monkeypatch.setattr(operation.api.requests, "request", lambda *a, **k: response({"media_id": "receipt"}))
    execute(ledger_path)

    def fail(*args, **kwargs):
        raise requests.Timeout("private-token")

    monkeypatch.setattr(operation.api.requests, "request", fail)
    assert execute(ledger_path, action="reconcile")["reconciliation"] == "unknown"
    monkeypatch.setattr(operation, "get_access_token", lambda **kw: pytest.fail("Status fetched token"))
    assert execute(ledger_path, action="status")["state"] == "acknowledged"
    assert execute(ledger_path)["request_started"] is False


def test_invalid_cli_and_init_never_dispatch(tmp_path, monkeypatch, capsys):
    path = tmp_path / "init.sqlite"
    monkeypatch.setattr(sys, "argv", ["draft_operation", "init", "--ledger", str(path)])
    monkeypatch.setattr(operation, "get_config", lambda *a: pytest.fail("Initialization accessed account"))
    assert operation.main() == 0
    assert json.loads(capsys.readouterr().out)["state"] == "initialized"
    assert operation.main() == 2  # exclusive init must not reset the ledger
    monkeypatch.setattr(sys, "argv", ["draft_operation", "submit", "--ledger", str(path)])
    assert operation.main() == 2


def test_killed_process_leaves_unknown_intent_without_network(ledger_path):
    source = """import sys,time,json
from draft_operation import DraftLedger
l=DraftLedger(sys.argv[1]); l.claim('task','private-app',json.loads(sys.argv[2]),submit=True)
print('committed',flush=True); time.sleep(60)
"""
    child = subprocess.Popen([sys.executable, "-c", source, str(ledger_path), json.dumps(PAYLOAD)],
                             cwd=Path(operation.__file__).parent, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "committed"
    finally:
        child.kill()
        child.wait(timeout=5)
        child.stdout.close()
    assert execute(ledger_path)["request_started"] is False


def test_missing_ledger_and_unknown_operation_do_not_initialize_implicitly(tmp_path, ledger_path):
    with pytest.raises(operation.sqlite3.Error):
        operation.DraftLedger(tmp_path / "missing.sqlite")
    with pytest.raises(operation.OperationConflict):
        execute(ledger_path, action="reconcile")


def test_capacity_is_not_evicted_and_unknown_schema_refused(ledger_path):
    ledger = operation.DraftLedger(ledger_path)
    with ledger.db:
        ledger.db.executemany("INSERT INTO operations VALUES(?,'owner','hash','unknown',NULL)",
                             [(str(i),) for i in range(10000)])
    with pytest.raises(operation.OperationConflict):
        ledger.claim("new", "private-app", PAYLOAD, submit=True)
    assert ledger.db.execute("SELECT count(*) FROM operations").fetchone()[0] == 10000
    ledger.db.execute("PRAGMA user_version=99")
    ledger.close()
    with pytest.raises(ValueError):
        operation.DraftLedger(ledger_path)


def test_cli_submit_and_status_return_sanitized_json(ledger_path, tmp_path, monkeypatch, capsys):
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(PAYLOAD), encoding="utf-8")
    args = ["draft_operation", "submit", "--ledger", str(ledger_path), "--operation", "task",
            "--account", "account", "--payload", str(payload_path)]
    monkeypatch.setattr(sys, "argv", args)
    monkeypatch.setattr(operation.api.requests, "request", lambda *a, **k: response({"media_id": "receipt"}))
    assert operation.main() == 0
    first = json.loads(capsys.readouterr().out)
    assert first["state"] == "acknowledged" and first["request_started"]
    args[1] = "status"
    assert operation.main() == 0
    assert not json.loads(capsys.readouterr().out)["request_started"]
