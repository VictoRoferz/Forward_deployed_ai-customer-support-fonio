"""Black-box checks for the ElevenLabs adapter against a RUNNING server.

    python test_elevenlabs_adapter.py [base_url]     # default http://localhost:8001

Style of test_verify_latency.py: stdlib + requests + dotenv, no framework.
Signs its own post-call payloads with ELEVENLABS_POST_CALL_HMAC_SECRET from
.env. Every ticket-creating case uses an agent id from
ELEVENLABS_TEST_AGENT_IDS, so tickets are always [TEST]/internal — the
script ABORTS if that variable is empty. It creates exactly TWO internal
Zammad tickets per run (full + minimal post-call); close them after the
session (CLAUDE.md rule). Case 12 (unset-secret -> 503) is a documented
manual check — it needs a server started without the secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import time
import uuid

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8001"
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
HMAC_SECRET = os.environ.get("ELEVENLABS_POST_CALL_HMAC_SECRET", "")
TEST_AGENT_IDS = [
    a.strip()
    for a in os.environ.get("ELEVENLABS_TEST_AGENT_IDS", "").split(",")
    if a.strip()
]

HEADERS = {"Content-Type": "application/json"}
if WEBHOOK_SECRET:
    HEADERS["Authorization"] = f"Bearer {WEBHOOK_SECRET}"

# The 11 dynamic variables the agent defines — call-init must return ALL of
# them on EVERY response (ElevenLabs requirement), and nothing else.
EXPECTED_VARS = {
    "customer_found", "name", "phone_number", "permission_to_order_again",
    "last_ordered_items", "last_order_date", "max_quantity", "contact_no",
    "customer_number", "verified", "last_request_number",
}

FIXTURE_PHONE = "+4981517703147"   # KN052805 (TEST INTERN Holger Haußmann)

failures: list[str] = []
created_tickets: list[tuple[str, object]] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{label:<38} {'ok' if ok else 'FAIL'}  {detail if not ok else ''}".rstrip())
    if not ok:
        failures.append(f"{label}: {detail}")


def sign(body: bytes, *, t: int | None = None, mac: str | None = None) -> str:
    t = int(time.time()) if t is None else t
    mac = mac or hmac.new(
        HMAC_SECRET.encode(), f"{t}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return f"t={t},v0={mac}"


def post_call(body: bytes, sig: str) -> requests.Response:
    return requests.post(
        f"{BASE}/elevenlabs/post-call",
        data=body,  # exact signed bytes — never let requests re-serialize
        headers={"Content-Type": "application/json", "ElevenLabs-Signature": sig},
        timeout=30,
    )


def call_init(payload: dict, headers: dict | None = None) -> requests.Response:
    return requests.post(
        f"{BASE}/elevenlabs/call-init",
        json=payload,
        headers=HEADERS if headers is None else headers,
        timeout=30,
    )


def transcription_payload(conv_id: str, *, minimal: bool = False) -> bytes:
    data: dict = {"agent_id": TEST_AGENT_IDS[0], "conversation_id": conv_id}
    if not minimal:
        data.update({
            "status": "done",
            "transcript": [
                {"role": "agent", "message": "Guten Tag, hier ist der MEDEL Assistent."},
                {"role": "user", "message": "[TEST] Simulierter Anruf."},
            ],
            "metadata": {
                "start_time_unix_secs": int(time.time()) - 60,
                "call_duration_secs": 60,
                "phone_call": {"external_number": FIXTURE_PHONE},
            },
            "analysis": {
                "call_successful": "success",
                "transcript_summary": "[TEST] Simulierter ElevenLabs-Anruf "
                                      "(test_elevenlabs_adapter.py) — bitte schließen.",
                "data_collection_results": {
                    "request_numbers": {"value": "72399999"},
                    "requested_items": {"value": "Batterien"},
                },
            },
            "conversation_initiation_client_data": {
                "dynamic_variables": {
                    "system__caller_id": FIXTURE_PHONE,
                    "phone_number": FIXTURE_PHONE,
                    "customer_found": True,
                    "name": "TEST INTERN Holger Haußmann",
                    "contact_no": "KN052805",
                    "customer_number": "4142028",
                    "verified": "true",
                    "permission_to_order_again": "true",
                    "last_request_number": "",
                },
            },
        })
    payload = {"type": "post_call_transcription",
               "event_timestamp": int(time.time()), "data": data}
    return json.dumps(payload).encode()


def main() -> int:
    if not HMAC_SECRET:
        print("ABORT: ELEVENLABS_POST_CALL_HMAC_SECRET is empty in .env — "
              "set a (dev) secret and restart the server, or post-call tests "
              "cannot be signed.")
        return 1
    if not TEST_AGENT_IDS:
        print("ABORT: ELEVENLABS_TEST_AGENT_IDS is empty in .env — refusing to "
              "run: post-call cases would create NON-internal Zammad tickets.")
        return 1

    print(f"testing ElevenLabs adapter at {BASE}\n")

    # 1. call-init, known fixture -> matched, full variable set, withholdings
    r = call_init({"caller_id": FIXTURE_PHONE, "agent_id": "agent_x",
                   "called_number": "+4900000000", "call_sid": "CA123"})
    body = r.json() if r.status_code == 200 else {}
    dv = body.get("dynamic_variables") or {}
    check("1 call-init fixture: 200 + envelope",
          r.status_code == 200
          and body.get("type") == "conversation_initiation_client_data",
          f"status {r.status_code}, body {str(body)[:120]}")
    check("1 call-init fixture: match + KN",
          dv.get("customer_found") is True and dv.get("contact_no") == "KN052805",
          f"vars {str(dv)[:160]}")
    check("1 call-init fixture: all 11 vars, no leaks",
          set(dv) == EXPECTED_VARS and dv.get("customer_number") == "",
          f"keys {sorted(set(dv) ^ EXPECTED_VARS)}, customer_number "
          f"{dv.get('customer_number')!r}")

    # 2. call-init, unknown number -> no match, IDENTICAL key set
    r = call_init({"caller_id": "+4900009999999"})
    dv = (r.json().get("dynamic_variables") or {}) if r.status_code == 200 else {}
    check("2 call-init unknown: no-match shape",
          r.status_code == 200 and dv.get("customer_found") is False
          and set(dv) == EXPECTED_VARS,
          f"status {r.status_code}, keys {sorted(set(dv) ^ EXPECTED_VARS)}")

    # 3. call-init, empty body (anonymous caller) -> same no-match shape
    r = call_init({})
    dv = (r.json().get("dynamic_variables") or {}) if r.status_code == 200 else {}
    check("3 call-init empty body: no-match shape",
          r.status_code == 200 and dv.get("customer_found") is False
          and set(dv) == EXPECTED_VARS,
          f"status {r.status_code}")

    # 4. call-init, wrong bearer -> 401 (only meaningful when a secret is set)
    if WEBHOOK_SECRET:
        r = call_init({"caller_id": FIXTURE_PHONE},
                      headers={"Content-Type": "application/json",
                               "Authorization": "Bearer wrong"})
        check("4 call-init wrong bearer: 401", r.status_code == 401,
              f"status {r.status_code}")
    else:
        print("4 call-init wrong bearer: SKIPPED (WEBHOOK_SECRET unset)")

    # 5. post-call, valid signature -> ticket created ([TEST]/internal)
    conv = f"conv_test_{uuid.uuid4().hex[:12]}"
    body5 = transcription_payload(conv)
    sig5 = sign(body5)
    r = post_call(body5, sig5)
    out = r.json() if r.status_code == 200 else {}
    check("5 post-call signed: created",
          r.status_code == 200 and out.get("created") is True
          and out.get("ticket_number"),
          f"status {r.status_code}, body {str(out)[:160]}")
    if out.get("ticket_number"):
        created_tickets.append((out["ticket_number"], out.get("ticket_id")))

    # 6. exact byte replay -> SAME ticket (dedup), no second creation
    r2 = post_call(body5, sig5)
    out2 = r2.json() if r2.status_code == 200 else {}
    check("6 post-call replay: same ticket (dedup)",
          r2.status_code == 200
          and out2.get("ticket_number") == out.get("ticket_number"),
          f"first {out.get('ticket_number')}, replay {out2.get('ticket_number')}")

    # 7. bad v0 -> 401
    r = post_call(body5, sign(body5, mac="ab" * 32))
    check("7 post-call bad v0: 401", r.status_code == 401, f"status {r.status_code}")

    # 8. stale timestamp (valid MAC for that timestamp) -> 401
    stale = int(time.time()) - 3600
    r = post_call(body5, sign(body5, t=stale))
    check("8 post-call stale t: 401", r.status_code == 401, f"status {r.status_code}")

    # 9. post_call_audio -> acknowledged but ignored
    audio = json.dumps({"type": "post_call_audio",
                        "event_timestamp": int(time.time()),
                        "data": {"agent_id": TEST_AGENT_IDS[0],
                                 "conversation_id": f"conv_test_{uuid.uuid4().hex[:12]}",
                                 "full_audio": "UklGRg=="}}).encode()
    r = post_call(audio, sign(audio))
    out = r.json() if r.status_code == 200 else {}
    check("9 post-call audio event: ignored",
          r.status_code == 200 and out.get("status") == "ignored"
          and "created" not in out,
          f"status {r.status_code}, body {str(out)[:120]}")

    # 10. unknown event type -> acknowledged but ignored
    unk = json.dumps({"type": "somewhere_new", "event_timestamp": int(time.time()),
                      "data": {}}).encode()
    r = post_call(unk, sign(unk))
    out = r.json() if r.status_code == 200 else {}
    check("10 post-call unknown type: ignored",
          r.status_code == 200 and out.get("status") == "ignored",
          f"status {r.status_code}, body {str(out)[:120]}")

    # 11. minimal data (agent_id + conversation_id only) -> still filed, internal
    body11 = transcription_payload(f"conv_test_{uuid.uuid4().hex[:12]}", minimal=True)
    r = post_call(body11, sign(body11))
    out = r.json() if r.status_code == 200 else {}
    check("11 post-call minimal data: created",
          r.status_code == 200 and out.get("created") is True
          and out.get("ticket_number"),
          f"status {r.status_code}, body {str(out)[:160]}")
    if out.get("ticket_number"):
        created_tickets.append((out["ticket_number"], out.get("ticket_id")))

    print("\n12 unset-secret -> 503: MANUAL (start the server without "
          "ELEVENLABS_POST_CALL_HMAC_SECRET and POST /elevenlabs/post-call)")

    if created_tickets:
        print("\ninternal [TEST] tickets created (close after the session):")
        for number, ticket_id in created_tickets:
            print(f"  #{number} (id {ticket_id})")

    if failures:
        print(f"\n{len(failures)} FAILURE(S):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nall ElevenLabs adapter checks green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
