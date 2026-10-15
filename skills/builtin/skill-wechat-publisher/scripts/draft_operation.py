"""Explicit durable draft submission and read-only receipt reconciliation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

import api
from config import get_config
from wechat_token import get_access_token

APP_ID = 0x45445731


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class OperationConflict(RuntimeError):
    pass


class DraftLedger:
    def __init__(self, path, *, create=False):
        path = Path(path)
        if not path.is_absolute():
            raise ValueError("Absolute ledger path required")
        if create:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        self.db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=5)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA synchronous=FULL")
            if create:
                with self.db:
                    self.db.execute("""CREATE TABLE operations (
                        id TEXT PRIMARY KEY, owner TEXT NOT NULL, fingerprint TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('unknown','acknowledged','failed')),
                        receipt TEXT)""")
                    self.db.execute(f"PRAGMA application_id={APP_ID}")
                    self.db.execute("PRAGMA user_version=1")
            if (self.db.execute("PRAGMA application_id").fetchone()[0] != APP_ID
                    or self.db.execute("PRAGMA user_version").fetchone()[0] != 1):
                raise ValueError("Unsupported ledger")
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def claim(self, operation, app_id, payload, *, submit):
        if (not isinstance(operation, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", operation)
                or not isinstance(app_id, str) or not app_id.strip()):
            raise ValueError("Invalid operation identity")
        if (not isinstance(payload, dict) or set(payload) != {"articles"}
                or not isinstance(payload["articles"], list) or not payload["articles"]
                or not all(isinstance(item, dict) and item for item in payload["articles"])):
            raise ValueError("Expected a nonempty articles payload")
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(raw.encode("utf-8")) > 1024 * 1024:
            raise ValueError("Payload too large")
        key, owner, fingerprint = digest(operation), digest(app_id), digest(raw)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            row = self.db.execute("SELECT * FROM operations WHERE id=?", (key,)).fetchone()
            if row:
                if row["owner"] != owner or row["fingerprint"] != fingerprint:
                    raise OperationConflict("Operation belongs to another account or payload")
                return dict(row), False
            if not submit:
                raise OperationConflict("Unknown operation; reconciliation never creates one")
            if self.db.execute("SELECT count(*) FROM operations").fetchone()[0] >= 10000:
                raise OperationConflict("Ledger capacity reached; no automatic eviction")
            self.db.execute("INSERT INTO operations VALUES(?,?,?,'unknown',NULL)", (key, owner, fingerprint))
        # The unknown intent is committed before any draft HTTP request can be sent.
        return {"id": key, "owner": owner, "fingerprint": fingerprint, "state": "unknown", "receipt": None}, True

    def record(self, row, state, receipt=None):
        if state not in {"failed", "acknowledged"}:
            raise ValueError("Invalid outcome")
        if state == "acknowledged" and (not isinstance(receipt, str) or not receipt.strip() or len(receipt) > 512):
            raise ValueError("Invalid receipt")
        with self.db:
            changed = self.db.execute("""UPDATE operations SET state=?,receipt=?
                WHERE id=? AND owner=? AND fingerprint=? AND state='unknown'""",
                (state, receipt, row["id"], row["owner"], row["fingerprint"])).rowcount
            if changed != 1:
                raise OperationConflict("Outcome already recorded")
        return {**row, "state": state, "receipt": receipt}


def execute(ledger, operation, payload, account, *, action="submit"):
    if action not in {"submit", "status", "reconcile"}:
        raise ValueError("Invalid operation")
    payload = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    config = get_config(account)
    app_id, account_key = config["app_id"], config["account_key"]
    row, claimed = ledger.claim(operation, app_id, payload, submit=action == "submit")
    result = {"state": row["state"], "request_started": False, "reconciliation": "not_requested"}
    if action == "status" or (action == "submit" and not claimed):
        return result
    if action == "reconcile" and not row["receipt"]:
        return {**result, "reconciliation": "unknown_no_receipt"}
    try:
        # The legacy token cache is keyed by alias, not app_id. Do not reuse a
        # cached token after an operator repoints that alias to another account.
        token = get_access_token(force_refresh=True, account_name=account_key)
        if get_config(account_key)["app_id"] != app_id:
            raise OperationConflict("Account changed before dispatch")
        if claimed:
            result["request_started"] = True
            receipt = api._create_draft_once(payload, token=token)
            row = ledger.record(row, "acknowledged", receipt)
            return {**result, "state": row["state"]}
        result["request_started"] = True
        response = api.requests.request("POST", f"{api.API_BASE}/draft/get?access_token={token}",
                                        json={"media_id": row["receipt"]}, timeout=api.API_TIMEOUT_SEC,
                                        allow_redirects=False)
        body = response.json() if response.status_code == 200 else None
        found = (isinstance(body, dict) and type(body.get("errcode", 0)) is int
                 and body.get("errcode", 0) == 0 and isinstance(body.get("news_item"), list)
                 and bool(body["news_item"]) and all(isinstance(item, dict) and item for item in body["news_item"]))
        return {**result, "reconciliation": "receipt_found" if found else "unknown"}
    except api.DraftRejected:
        row = ledger.record(row, "failed")
        return {**result, "state": row["state"]}
    except (api.DraftOutcomeUnknown, api.requests.RequestException, ValueError, RuntimeError):
        # Do not print token-bearing URLs, provider bodies, account details or content.
        return {**result, "reconciliation": "unknown"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "submit", "status", "reconcile"])
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--operation")
    parser.add_argument("--payload", help="UTF-8 JSON file containing the original articles payload")
    parser.add_argument("--account")
    args = parser.parse_args()
    ledger = None
    try:
        if args.action != "init" and (not args.operation or not args.payload or not args.account):
            raise ValueError("Operation, payload and explicit account required")
        ledger = DraftLedger(args.ledger, create=args.action == "init")
        if args.action == "init":
            print('{"state":"initialized"}')
            return 0
        with open(args.payload, "rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError("Payload too large")
        result = execute(ledger, args.operation, json.loads(raw), args.account, action=args.action)
        print(json.dumps(result, sort_keys=True))
        return 0 if (result["reconciliation"] == "receipt_found" or
                     args.action != "reconcile" and result["state"] == "acknowledged") else 1
    except (OSError, ValueError, RuntimeError, sqlite3.Error, KeyError):
        print('{"state":"unavailable_or_conflicting","automatic_retry":false}')
        return 2
    finally:
        if ledger:
            ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
