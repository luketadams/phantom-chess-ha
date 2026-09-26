"""Smoke-test the integration in a throwaway Home Assistant container.

Drives onboarding, the real config flow (manual MAC, optional token), the
engine preflight, and entry removal over HA's REST API. No board is needed:
Bluetooth simply never connects. See tests/README.md for the docker recipe.

    python3 scripts/ha_e2e.py onboard            # first run: onboard + add entry
    python3 scripts/ha_e2e.py engine <entry_id>  # download + verify + ping Stockfish
    python3 scripts/ha_e2e.py remove <entry_id>
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.environ.get("HA", "http://localhost:18123")
CLIENT_ID = BASE + "/"
PASSWORD = "test-pass-1234"
TOKEN = None


def call(method, path, body=None, form=False):
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    data = None
    if body is not None:
        if form:
            data = urllib.parse.urlencode(body).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode()[:800]


def wait_up():
    for _ in range(180):
        try:
            with urllib.request.urlopen(BASE + "/manifest.json", timeout=5):
                return
        except Exception:
            time.sleep(2)
    sys.exit("Home Assistant did not come up")


def _token_from_code(code):
    global TOKEN
    status, tok = call("POST", "/auth/token", {"grant_type": "authorization_code", "code": code, "client_id": CLIENT_ID}, form=True)
    assert status == 200, (status, tok)
    TOKEN = tok["access_token"]


def onboard():
    status, res = call("POST", "/api/onboarding/users", {
        "client_id": CLIENT_ID, "name": "Test", "username": "test", "password": PASSWORD, "language": "en"})
    assert status == 200, (status, res)
    _token_from_code(res["auth_code"])
    call("POST", "/api/onboarding/core_config", {})


def login():
    _, flow = call("POST", "/auth/login_flow", {"client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": CLIENT_ID})
    _, res = call("POST", f"/auth/login_flow/{flow['flow_id']}", {"client_id": CLIENT_ID, "username": "test", "password": PASSWORD})
    _token_from_code(res["result"])


def add_entry(mac, token=""):
    _, flow = call("POST", "/api/config/config_entries/flow", {"handler": "phantom_chess", "show_advanced_options": False})
    print("step:", flow.get("step_id"), "schema:", flow.get("data_schema"))
    _, flow = call("POST", f"/api/config/config_entries/flow/{flow['flow_id']}", {"ble_address": mac})
    print("step:", flow.get("step_id"), "errors:", flow.get("errors"))
    _, flow = call("POST", f"/api/config/config_entries/flow/{flow['flow_id']}", {"lichess_token": token} if token else {})
    print("result:", flow.get("type"), flow.get("title"))
    return flow["result"]["entry_id"]


def board_states():
    _, states = call("GET", "/api/states")
    return sorted((s for s in states if s["entity_id"].split(".")[1].startswith(("phantom", "aa_bb", "c8_c9"))
                   or "_aa_bb_" in s["entity_id"]), key=lambda s: s["entity_id"])


def main(step, *args):
    wait_up()
    onboard() if step == "onboard" else login()
    if step == "onboard":
        entry_id = add_entry("AA:BB:CC:DD:EE:FF")
        print("ENTRY", entry_id)
        time.sleep(8)
        states = board_states()
        print("entities:", len(states), "unavailable:", sum(s["state"] == "unavailable" for s in states))
        for s in states:
            print("  ", s["entity_id"], s["state"])
    elif step == "engine":
        started = time.time()
        status, res = call("POST", "/api/services/phantom_chess/check_engine?return_response", {"entry_id": args[0]})
        print("check_engine", status, json.dumps(res)[:800], f"{time.time() - started:.0f}s")
    elif step == "remove":
        print("remove", call("DELETE", f"/api/config/config_entries/entry/{args[0]}"))
    elif step == "entries":
        print(call("GET", "/api/config/config_entries/entry?domain=phantom_chess"))


if __name__ == "__main__":
    main(*sys.argv[1:])
