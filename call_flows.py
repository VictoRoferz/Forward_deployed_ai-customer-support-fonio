"""Platform-neutral call-flow cores shared by every voice platform.

The Fonio routes (main.py) and the ElevenLabs adapter (adapter_elevenlabs.py)
both delegate to the functions here, so the on-ring lookup and the after-call
protocol ticket behave identically regardless of which platform fronts the
call. Platform-specific I/O shapes (Fonio's template-tolerant input models,
ElevenLabs' dynamic-variables envelope and HMAC) stay in the respective
route modules.

Layering note: unlike conn_* / verification.py (pure, framework-free), this
module deliberately raises fastapi.HTTPException — it is the extracted
HTTP-policy layer, not a connector. Keeping the existing exception→status
mappings (BCConfigError→500, ZammadConfigError→500, ZammadError→502) inside
the shared cores gives every platform identical status semantics; the 502 on
a Zammad failure is also what makes ElevenLabs retry its post-call webhook.
"""

from __future__ import annotations

import logging
import os

from fastapi import HTTPException
from pydantic import BaseModel

from conn_business_central import (
    BCConfigError,
    check_order_eligibility,
    get_contact_by_no,
    lookup_by_phone,
)
from conn_zammad import ZammadConfigError, ZammadError, create_call_ticket
from verification import is_minor

# Same logger as main.py so extracted code logs exactly as before.
log = logging.getLogger("fonio-bc")

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET")

# Mirrors main.VERIFY_EXCLUDE_ARCHIVED (read here too: this module must not
# import main). Archived BC rows are obsolete records, never identities.
VERIFY_EXCLUDE_ARCHIVED = os.environ.get("VERIFY_EXCLUDE_ARCHIVED", "1") == "1"

# Max units per spare-part request. Echoed as {{max_quantity}} in /lookup-caller
# and enforced server-side in /create-request (§11.2).
SPARE_MAX_QUANTITY = int(os.environ.get("SPARE_MAX_QUANTITY", "5"))


def _check_auth(authorization: str | None) -> None:
    if not WEBHOOK_SECRET:
        return
    expected = f"Bearer {WEBHOOK_SECRET}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="unauthorized")


class CallerOut(BaseModel):
    """Response for /lookup-caller. Top-level keys are bound 1:1 to the Fonio
    prompt's {{system variables}} (§9) — DO NOT rename without updating the prompt.

    open_requests is always null in v1 (duplicate prevention runs server-side in
    /create-request instead — the on-ring path has no latency budget for it).
    authorized_contacts is always null (no BC data source; documented as unbacked).
    emergency_number / alternative_channel are static Fonio-side variables, never
    returned here. Extra keys (contact_no, health_insurance_no, last_order_date)
    are for internal use / the Zammad protocol and are ignored by the prompt.
    """

    customer_found: bool
    name: str | None = None
    customer_number: str | None = None          # BC customerNo (for orders)
    date_of_birth: str | None = None
    phone_number: str | None = None             # echoed input
    postal_code: str | None = None              # BC postCode (verification factor)
    permission_to_order_again: bool | None = None
    last_ordered_items: str = ""
    open_requests: str | None = None            # always null in v1
    authorized_contacts: str | None = None      # always null (unbacked)
    max_quantity: int = SPARE_MAX_QUANTITY
    # Under 18 by BC birth date (None = unknown/no date): the agent must confirm
    # a parent/guardian is calling before disclosing anything (§10.2).
    patient_is_minor: bool | None = None
    # False lets the agent skip the birth-date question (20% of patients have
    # none on file) and go straight to Kundennummer/PLZ. Reveals only whether
    # a date exists, never the date — and only for a phone-matched caller.
    dob_on_file: bool | None = None
    # internal / protocol only:
    contact_no: str | None = None               # BC KN-number (Zammad linkage)
    health_insurance_no: str | None = None      # reported, not a decision
    last_order_date: str | None = None


class CallLogOut(BaseModel):
    created: bool
    ticket_id: int | None = None
    ticket_number: str | None = None


def _verified_label(verified: bool | None) -> str:
    return "nicht geprüft" if verified is None else ("ja" if verified else "nein")


def _patient_for(contact: dict) -> dict | None:
    """Map a phone hit to the PATIENT it belongs to (BC hierarchy, 2026-08-28):
    a number stored on a sub-contact (a parent's row, a school) identifies the
    account's primary card, not the sub-contact — the caller then verifies with
    the patient's data as usual. Archived rows (obsolete records) yield None,
    so the caller is treated as unknown. One extra BC query only when the hit
    is a sub-contact."""
    if VERIFY_EXCLUDE_ARCHIVED and contact.get("archived"):
        return None
    if contact.get("is_primary"):
        return contact
    primary = get_contact_by_no(contact.get("company_no") or "")
    if not primary or (VERIFY_EXCLUDE_ARCHIVED and primary.get("archived")):
        return None
    log.info("ring: sub-contact %s (%s) -> patient %s", contact.get("no"),
             contact.get("relatives_status") or "no relation", primary.get("no"))
    return primary


def lookup_caller_core(phone_number: str | None) -> CallerOut:
    """On call ring: identify the caller by phone in BC and, if matched, compute
    spare-part order eligibility (3-month recency rule). Returns all the prompt's
    system variables in one response. The agent still verifies identity (name +
    date_of_birth) against these values before disclosing anything."""
    try:
        contact = lookup_by_phone(phone_number)
    except BCConfigError as e:
        raise HTTPException(status_code=500, detail=f"config: {e}") from e
    except Exception:
        # Graceful degrade (incl. BCAuthError): a 5xx here makes Fonio drop the
        # webhook and start the call with NO context at all, so any BC failure
        # (e.g. the 2026-07-27 unpublished-web-services outage) becomes an
        # "unknown caller" instead — the agent falls back to name/Kundennummer
        # verification, which fails loudly (502) on its own if BC is down.
        # Detection lives in warmup()/GET /health/deep, not in dropped calls.
        log.exception("lookup_by_phone failed; degrading to customer_found=false")
        return CallerOut(customer_found=False, phone_number=phone_number)

    if contact:
        try:
            contact = _patient_for(contact)
        except Exception:
            log.exception("primary-contact resolution failed; degrading to customer_found=false")
            contact = None
    if not contact:
        return CallerOut(customer_found=False, phone_number=phone_number)

    # Eligibility is best-effort (never raises) so a slow/failed sales query
    # degrades to permission=None rather than dropping the call.
    elig = check_order_eligibility(contact.get("customer_no") or "")

    return CallerOut(
        customer_found=True,
        name=contact["name"],
        # customer_number withheld since 2026-07-20: a spoken Kundennummer now
        # verifies on its own, so the LLM must never hold the value — an echoed
        # ring variable could otherwise verify a caller who never said it.
        customer_number=None,
        # Deliberately withheld since the unified flow (2026-07-13): the DOB is
        # the verification secret and is checked server-side in /verify-caller —
        # if the LLM never receives it, no prompt injection can leak it.
        date_of_birth=None,
        phone_number=phone_number,
        postal_code=contact.get("postal_code"),
        permission_to_order_again=elig["permission_to_order_again"],
        last_ordered_items=elig["last_ordered_items"],
        patient_is_minor=is_minor(contact.get("birth_date")),
        dob_on_file=bool(contact.get("birth_date")),
        contact_no=contact["no"],
        health_insurance_no=contact.get("health_insurance_no"),
        last_order_date=elig["last_order_date"],
    )


def log_call_core(
    *,
    phone_number: str | None = None,
    name: str | None = None,
    customer: str | None = None,
    summary: str | None = None,
    contact_no: str | None = None,
    bc_found: bool | None = None,
    title: str | None = None,
    verified: bool | None = None,
    request_numbers: str | None = None,
    requested_items: str | None = None,
    eligibility: str | None = None,
    internal: bool = False,
    source: str = "fonio",
    extra_lines: dict[str, object] | None = None,
) -> CallLogOut:
    """Create the ONE Zammad ticket logging a finished call (after-call path,
    no live-call latency budget).

    `source="fonio"` keeps the historical ticket bytes exactly (no tags, the
    default Fonio article subject); other platforms get their own article
    subject plus a source tag so tickets stay filterable per platform.
    `extra_lines` appends platform-specific protocol lines (e.g. the
    ElevenLabs conversation id) after the standard ones."""
    if source == "fonio":
        subject, tags = None, None
    else:
        label = {"elevenlabs": "ElevenLabs"}.get(source, source)
        subject = f"Eingehender Anruf ({label} AI)"
        tags = source + (",test" if internal else "")
    # Full protocol lines (decision 2026-07-27) — the human sees the whole call
    # in one ticket and can cross-open the request tickets by number. This is
    # also the backstop for the accepted same-call duplicate-lag risk: if two
    # identical requests evaded DUPLICATE_OPEN, both numbers are listed here.
    # PII rule unchanged: no insurance number, no device serials.
    extra = {
        "Identität verifiziert": _verified_label(verified),
        "Angelegte Vorgänge": request_numbers,
        "Angefragte Artikel": requested_items,
        "Bestellberechtigung (90-Tage-Regel)": {
            "true": "ja", "false": "nein"
        }.get((eligibility or "").strip().lower(), eligibility),
    }
    if extra_lines:
        extra.update(extra_lines)
    try:
        ticket = create_call_ticket(
            phone_number=phone_number,
            name=name,
            customer=customer,
            summary=summary,
            contact_no=contact_no,
            bc_found=bc_found,
            title=title,
            extra=extra,
            internal=internal,
            subject=subject,
            tags=tags,
        )
    except ZammadConfigError as e:
        raise HTTPException(status_code=500, detail=f"zammad config: {e}") from e
    except ZammadError as e:
        log.exception("create_call_ticket failed")
        raise HTTPException(status_code=502, detail=f"zammad: {e}") from e

    return CallLogOut(
        created=True,
        ticket_id=ticket.get("id") if ticket else None,
        ticket_number=ticket.get("number") if ticket else None,
    )
