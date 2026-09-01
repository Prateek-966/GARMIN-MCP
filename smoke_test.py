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
from garminconnect import (  # noqa: E402
    GarminConnectAuthenticationError,
    GarminConnectNotFoundError,
)
import uvicorn  # noqa: E402

import server  # noqa: E402


class FakeGarmin:
    """Stands in for garminconnect.Garmin; records what the tools asked for."""

    calls: list[tuple[str, tuple[Any, ...]]] = []

    # Methods that raise, to model Garmin 404ing a metric the day or activity
    # genuinely has no data for.
    missing: set[str] = {"get_activity_weather", "get_hydration_data"}

    def __getattr__(self, name: str):
        def method(*args: Any, **kwargs: Any) -> Any:
            FakeGarmin.calls.append((name, args))
            if name in FakeGarmin.missing:
                raise GarminConnectNotFoundError(f"404 for {name}")
            if name == "get_activities":
                return [{
                    "activityId": 1, "activityName": "Morning Run",
                    "distance": 5000.0, "averageHR": 148, "dropMe": None,
                    "notInAllowlist": "should be filtered",
                }]
            if name == "get_user_profile":
                return {"userProfileNumber": 998877, "displayName": "runner"}
            if name == "get_full_name":
                return "Test Runner"
            if name == "get_respiration_data":
                return {"avgSleepRespirationValue": 14.2,
                        "respirationValuesArray": [[1, 2]] * 400}
            if name == "get_heart_rates":
                return {"restingHeartRate": 48, "heartRateValues": [[1, 60]] * 400}
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
            "activity_breakdown", "wellness_detail", "fitness_metrics",
            "personal_records", "devices", "gear", "user_profile", "workouts",
            "progress_summary", "list_raw_methods",
        }
        check("all 26 tools are registered", names == expected, f"missing {sorted(expected - names)} extra {sorted(names - expected)}")
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

        # --- composite tools -------------------------------------------------
        res = rpc(client, "tools/call", {"name": "activity_breakdown", "arguments": {"activity_id": 42}})
        payload = json.loads(res["result"]["content"][0]["text"])
        check("activity_breakdown returns the sections that exist",
              "splits" in payload and "hr_zones" in payload, str(payload)[:200])
        check("activity_breakdown reports a 404 section without failing the call",
              "unavailable" in str(payload.get("weather", "")), str(payload)[:200])
        check("activity_breakdown passes the id as a string",
              ("get_activity_splits", ("42",)) in FakeGarmin.calls)

        before = len([c for c in FakeGarmin.calls if c[0] == "get_activity_weather"])
        check("a 404 does not trigger a re-auth retry", before == 1, f"called {before}x")

        res = rpc(client, "tools/call", {"name": "wellness_detail", "arguments": {}})
        payload = json.loads(res["result"]["content"][0]["text"])
        check("wellness_detail keeps summary figures",
              payload.get("heart_rates", {}).get("restingHeartRate") == 48, str(payload)[:200])
        check("wellness_detail drops minute-by-minute arrays",
              "heartRateValues" not in str(payload) and "respirationValuesArray" not in str(payload))

        res = rpc(client, "tools/call", {"name": "gear", "arguments": {}})
        check("gear looks up the profile number the API needs",
              ("get_gear", ("998877",)) in FakeGarmin.calls, str(FakeGarmin.calls)[-300:])

        res = rpc(client, "tools/call", {"name": "gear", "arguments": {"gear_uuid": "abc"}})
        check("gear with a uuid fetches that item's stats",
              ("get_gear_stats", ("abc",)) in FakeGarmin.calls)

        res = rpc(client, "tools/call", {"name": "user_profile", "arguments": {}})
        payload = json.loads(res["result"]["content"][0]["text"])
        check("user_profile combines name, units and profile",
              payload.get("full_name") == "Test Runner" and "profile" in payload, str(payload)[:200])

        res = rpc(client, "tools/call", {"name": "list_raw_methods", "arguments": {}})
        text = res["result"]["content"][0]["text"]
        check("list_raw_methods enumerates real client methods",
              "get_floors" in text and "get_menstrual_calendar_data" in text, text[:150])
        check("list_raw_methods excludes non-get_ methods",
              "add_weigh_in" not in text and "delete_workout" not in text)

        # An expired session, unlike a 404, is worth exactly one re-login.
        attempts = {"n": 0}

        def flaky(*_a: Any, **_k: Any) -> Any:
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise GarminConnectAuthenticationError("401 Unauthorized")
            return {"recovered": True}

        rebuilds = {"n": 0}

        def counting_build() -> Any:
            rebuilds["n"] += 1
            fake = FakeGarmin()
            object.__setattr__(fake, "get_rhr_day", flaky)
            return fake

        server._build_client = counting_build
        server._client = None
        res = rpc(client, "tools/call", {"name": "resting_heart_rate", "arguments": {}})
        text = res["result"]["content"][0]["text"]
        check("an expired session triggers one re-auth and recovers",
              "recovered" in text and attempts["n"] == 2, f"{text[:80]} attempts={attempts['n']}")
        check("re-auth builds exactly one fresh client", rebuilds["n"] == 2, str(rebuilds))
        server._build_client = lambda: FakeGarmin()
        server._client = None

        res = rpc(client, "tools/call", {"name": "fitness_metrics", "arguments": {}})
        payload = json.loads(res["result"]["content"][0]["text"])
        check("fitness_metrics gathers the physiological set",
              {"endurance_score", "hill_score", "cycling_ftp"} <= set(payload), str(payload)[:200])

    uv.should_exit = True
    print()
    if failures:
        print(f"{len(failures)} check(s) failed: {', '.join(failures)}")
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
