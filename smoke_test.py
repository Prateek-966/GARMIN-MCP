"""
End-to-end smoke test. No Garmin account or network needed — the Garmin client
is replaced with a stub, so this checks the parts that break silently in a
deploy: key auth, the MCP handshake, tool registration, and response shaping.

    pip install -r requirements.txt httpx
    python smoke_test.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from typing import Any

TOKENS = {"di_token": "t", "di_refresh_token": "r", "di_client_id": "c"}
KEY = "smoke-test-key-at-least-24-characters-long"
PORT = int(os.environ.get("SMOKE_PORT", "8765"))

os.environ["GARMIN_TOKENS"] = base64.b64encode(json.dumps(TOKENS).encode()).decode()
os.environ["MCP_KEY"] = KEY
os.environ.pop("RENDER_EXTERNAL_HOSTNAME", None)

import httpx  # noqa: E402
import uvicorn  # noqa: E402

import server  # noqa: E402


class FakeGarmin:
    """Stands in for garminconnect.Garmin; records what the tools asked for."""

    calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str):
        def method(*args: Any, **kwargs: Any) -> Any:
            FakeGarmin.calls.append((name, args))
            if name == "get_activities":
                return [{
                    "activityId": 1, "activityName": "Morning Run",
                    "distance": 5000.0, "averageHR": 148, "dropMe": None,
                    "notInAllowlist": "should be filtered",
                }]
            return {"date": args[0] if args else None, "value": 42, "empty": None}

        return method


server._build_client = lambda: FakeGarmin()  # type: ignore[assignment]

BASE = f"http://127.0.0.1:{PORT}"
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-06-18",
}

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}{'  — ' + detail if detail and not ok else ''}")
    if not ok:
        failures.append(label)


def rpc(client: httpx.Client, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    r = client.post(f"{BASE}/mcp?key={KEY}", json=body, headers=MCP_HEADERS, timeout=30)
    r.raise_for_status()
    text = r.text
    if text.startswith("event:") or "\ndata: " in text:  # SSE framing
        text = next(l[6:] for l in text.splitlines() if l.startswith("data: "))
    return json.loads(text)


def main() -> int:
    config = uvicorn.Config(server.app, host="127.0.0.1", port=PORT, log_level="warning")
    uv = uvicorn.Server(config)
    threading.Thread(target=uv.run, daemon=True).start()

    deadline = time.time() + 30
    while not uv.started and time.time() < deadline:
        time.sleep(0.1)
    if not uv.started:
        print("FAIL  server did not start")
        return 1

    with httpx.Client() as client:
        r = client.get(f"{BASE}/healthz", timeout=10)
        check("healthz is open and returns ok", r.status_code == 200 and r.json() == {"status": "ok"}, r.text)

        r = client.post(f"{BASE}/mcp", json={}, headers=MCP_HEADERS, timeout=10)
        check("/mcp rejects a request with no key", r.status_code == 401, f"got {r.status_code}")

        r = client.post(f"{BASE}/mcp?key=wrong", json={}, headers=MCP_HEADERS, timeout=10)
        check("/mcp rejects a wrong key", r.status_code == 401, f"got {r.status_code}")

        r = client.post(
            f"{BASE}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={**MCP_HEADERS, "Authorization": f"Bearer {KEY}"}, timeout=30,
        )
        check("Bearer header is accepted as auth", r.status_code == 200, f"got {r.status_code}")

        init = rpc(client, "initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "smoke-test", "version": "0"},
        })
        check("initialize handshake succeeds", init.get("result", {}).get("serverInfo", {}).get("name") == "garmin", json.dumps(init)[:200])

        listed = rpc(client, "tools/list")
        names = {t["name"] for t in listed.get("result", {}).get("tools", [])}
        expected = {
            "list_activities", "activities_by_date", "activity_detail", "daily_summary",
            "sleep", "hrv", "training_readiness", "training_status", "body_battery",
            "stress", "resting_heart_rate", "vo2_max", "race_predictions",
            "body_composition", "steps_range", "raw_get",
        }
        check("all 16 tools are registered", names == expected, f"missing {sorted(expected - names)} extra {sorted(names - expected)}")
        check("every tool has a description",
              all(t.get("description") for t in listed.get("result", {}).get("tools", [])))

        res = rpc(client, "tools/call", {"name": "list_activities", "arguments": {"limit": 3}})
        text = res["result"]["content"][0]["text"]
        payload = json.loads(text)
        check("list_activities returns pruned activities",
              payload and payload[0].get("activityName") == "Morning Run", text[:200])
        check("nulls are dropped", "dropMe" not in payload[0])
        check("non-allowlisted fields are dropped", "notInAllowlist" not in payload[0])
        check("list_activities passes (offset, limit) in that order",
              ("get_activities", (0, 3)) in FakeGarmin.calls,
              str(FakeGarmin.calls))

        res = rpc(client, "tools/call", {"name": "daily_summary", "arguments": {}})
        called = [a for n, a in FakeGarmin.calls if n == "get_user_summary"]
        check("daily_summary defaults to today", bool(called) and called[0][0] == server.today(), str(called))

        res = rpc(client, "tools/call", {"name": "raw_get", "arguments": {"method": "set_weight"}})
        check("raw_get refuses non-get_* methods",
              "Only read-only" in res["result"]["content"][0]["text"])

        res = rpc(client, "tools/call", {"name": "raw_get", "arguments": {"method": "get_device_last_used"}})
        check("raw_get allows get_* methods",
              any(n == "get_device_last_used" for n, _ in FakeGarmin.calls))

    uv.should_exit = True
    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
