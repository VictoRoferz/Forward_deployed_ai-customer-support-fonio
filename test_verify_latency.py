"""Live latency + §10.1 regression replay for /verify-caller and /lookup-caller.

Read-only: hits ONLY the two lookup/verify endpoints of a RUNNING server (start
one with `uvicorn main:app --port 8001`) against the real DE-TEST tenant — it
never calls /create-request, so no Zammad tickets are created.

What it proves after the 2026-08-10 latency retrofit (pooled session +
concurrent BC fetches):
  1. The documented §10.1 verification decisions are unchanged (Mettmann,
     Hans-Wurst KN-alone, shared Kundennummer 4110082, conflict case).
  2. ALL failure responses are byte-identical (callers must never learn WHY
     verification failed, nor whether a record exists) — the script prints the
     body hash so it can be diffed against a pre-change capture.
  3. Latency is inside Fonio's ~5 s tool abort with margin (targets are soft:
     BC weather varies; correctness failures are hard).

Usage:
    python test_verify_latency.py [base_url]        # default http://localhost:8001

Exit code 0 = all correctness checks green; 1 = at least one failed.
"""

from __future__ import annotations

import hashlib
import os
import statistics
import sys
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8001"
SECRET = os.environ.get("WEBHOOK_SECRET", "")
HEADERS = {"Content-Type": "application/json"}
if SECRET:
    HEADERS["Authorization"] = f"Bearer {SECRET}"

RUNS = 3  # timing runs per case

# (label, endpoint, body, expectations, soft latency target in seconds)
# Expectation keys are compared against the response JSON; None means "must be
# null". Fixtures: see CLAUDE.md "Test persons" + the 2026-07-28 Brongkoll notes.
CASES = [
    ("dead_body_kn_ladder", "/verify-caller",
     {"name": "Annelore Mettmann", "date_of_birth": "05.07.1985",
      "phone_number": "+491737493489", "customer_number": "4137079", "postal_code": ""},
     {"verified": True, "contact_no": "KN045531"}, 2.5),
    ("clean_name_dob", "/verify-caller",
     {"name": "Hannelore Mettmann", "date_of_birth": "05.07.1985",
      "phone_number": "", "customer_number": "", "postal_code": ""},
     {"verified": True, "contact_no": "KN045531"}, 2.0),
    ("fail_wrong_firstname", "/verify-caller",
     {"name": "Annelore Mettmann", "date_of_birth": "05.07.1985",
      "phone_number": "+491737493489", "customer_number": "", "postal_code": ""},
     {"verified": False}, 2.0),
    ("fail_unknown_name", "/verify-caller",
     {"name": "Xaver Qwertzson", "date_of_birth": "01.01.1980",
      "phone_number": "", "customer_number": "", "postal_code": ""},
     {"verified": False}, 2.0),
    ("kn_alone_unique", "/verify-caller",
     {"name": "", "date_of_birth": "", "phone_number": "",
      "customer_number": "4158636", "postal_code": ""},
     {"verified": True, "contact_no": "KN083495"}, 2.0),
    ("fail_shared_kn_alone", "/verify-caller",
     {"name": "", "date_of_birth": "", "phone_number": "",
      "customer_number": "4110082", "postal_code": ""},
     {"verified": False}, 2.0),
    ("shared_kn_plus_dob", "/verify-caller",
     {"name": "", "date_of_birth": "19.08.1994", "phone_number": "",
      "customer_number": "4110082", "postal_code": ""},
     {"verified": True, "contact_no": "KN002616"}, 2.0),
    ("fail_conflicting_claims", "/verify-caller",
     {"name": "Hannelore Mettmann", "date_of_birth": "05.07.1985",
      "phone_number": "", "customer_number": "4158636", "postal_code": ""},
     {"verified": False}, 2.0),
    ("ring_lookup_match", "/lookup-caller",
     {"phone_number": "+4981517703147"},
     {"customer_found": True, "contact_no": "KN052805"}, 2.0),
    ("ring_lookup_unknown", "/lookup-caller",
     {"phone_number": "+4930111222333"},
     {"customer_found": False}, 2.0),
]


def call(endpoint: str, body: dict) -> tuple[dict, bytes, float]:
    resp = requests.post(BASE + endpoint, json=body, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json(), resp.content, resp.elapsed.total_seconds()


def main() -> int:
    failures: list[str] = []
    fail_bodies: dict[str, bytes] = {}

    print(f"replaying {len(CASES)} cases x{RUNS} against {BASE}\n")
    print(f"{'case':<26} {'result':<8} {'min':>6} {'median':>7} {'max':>6}  target")
    for label, endpoint, body, expect, target in CASES:
        times: list[float] = []
        data: dict = {}
        content = b""
        ok = True
        for _ in range(RUNS):
            try:
                data, content, elapsed = call(endpoint, body)
            except Exception as e:  # noqa: BLE001 — a dead server fails the run
                failures.append(f"{label}: request error {e}")
                ok = False
                break
            times.append(elapsed)
        if not ok:
            print(f"{label:<26} {'ERROR':<8}")
            continue

        for key, want in expect.items():
            got = data.get(key)
            if got != want:
                failures.append(f"{label}: expected {key}={want!r}, got {got!r}")
                ok = False
        if endpoint == "/verify-caller" and expect.get("verified") is False:
            fail_bodies[label] = content

        med = statistics.median(times)
        slow = "  SLOW" if med > target else ""
        print(f"{label:<26} {'ok' if ok else 'FAIL':<8} "
              f"{min(times):>5.2f}s {med:>6.2f}s {max(times):>5.2f}s  <={target}s{slow}")

    # §10.1: every failure body must be byte-identical (and match the
    # pre-change capture — compare the printed hash against it manually).
    if fail_bodies:
        unique = {b for b in fail_bodies.values()}
        digest = hashlib.sha256(next(iter(fail_bodies.values()))).hexdigest()[:16]
        if len(unique) == 1:
            print(f"\n§10.1 failure bodies: byte-identical across "
                  f"{len(fail_bodies)} cases, sha256 {digest}…")
        else:
            failures.append("§10.1 VIOLATION: failure bodies differ between cases")
            print("\n§10.1 VIOLATION: failure bodies are NOT identical:")
            for label, b in fail_bodies.items():
                print(f"  {label}: sha256 {hashlib.sha256(b).hexdigest()[:16]}… "
                      f"({len(b)} bytes)")

    # Concurrency smoke: three different requests at once must not cross talk.
    smoke = [CASES[0], CASES[4], CASES[8]]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda c: call(c[1], c[2])[0], smoke))
    for (label, _, _, expect, _), data in zip(smoke, results):
        for key, want in expect.items():
            if data.get(key) != want:
                failures.append(f"concurrent {label}: {key}={data.get(key)!r}, "
                                f"expected {want!r}")
    print("concurrency smoke (3 parallel requests): "
          + ("ok" if not any(f.startswith("concurrent") for f in failures) else "FAIL"))

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nall correctness checks green")
    return 0


if __name__ == "__main__":
    sys.exit(main())
