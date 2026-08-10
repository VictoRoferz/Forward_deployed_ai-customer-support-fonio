"""Business Central OData connector.

Supports two auth modes — configured via env vars:

  OAuth2 (BC Online/SaaS, recommended):
    BC_TENANT_ID, BC_CLIENT_ID, BC_CLIENT_SECRET

  Basic Auth (BC On-Premises with Web Service Access Key):
    BC_USERNAME, BC_ACCESS_KEY

OData endpoint:
    BC_CONTACTS_URL — full URL to the contact entity set.
    e.g. https://api.businesscentral.dynamics.com/v2.0/{tenant}/{env}/ODataV4/Company('MyCo')/ContactCard
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import date, timedelta
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

log = logging.getLogger("fonio-bc.connector")

BC_CONTACTS_URL = os.environ.get("BC_CONTACTS_URL", "").rstrip("/")

BC_TENANT_ID = os.environ.get("BC_TENANT_ID")
BC_CLIENT_ID = os.environ.get("BC_CLIENT_ID")
BC_CLIENT_SECRET = os.environ.get("BC_CLIENT_SECRET")
BC_OAUTH_SCOPE = os.environ.get(
    "BC_OAUTH_SCOPE", "https://api.businesscentral.dynamics.com/.default"
)

BC_USERNAME = os.environ.get("BC_USERNAME")
BC_ACCESS_KEY = os.environ.get("BC_ACCESS_KEY")

PHONE_FIELDS = [
    f.strip()
    for f in os.environ.get(
        "BC_PHONE_FIELDS",
        "phoneNo,mobilePhoneNo,phoneNo2,mobilePhoneNo2,privatPhoneNo,privatMobilePhoneNo",
    ).split(",")
    if f.strip()
]

DEFAULT_COUNTRY_CODE = os.environ.get("BC_DEFAULT_COUNTRY_CODE", "49")

# Contact field holding the postal code (verification factor). Confirmed live on
# DE-TEST: `postCode` (top-level, populated; the privat* block is empty there).
# Empty value disables the field entirely — important because a wrong name here
# would 400 EVERY lookup (there is no bad-field recovery in _odata_get).
BC_POSTAL_FIELD = os.environ.get("BC_POSTAL_FIELD", "postCode").strip()

# "Ordered recently?" window for spare-part eligibility. 3 months ≈ 90 days.
ORDER_WINDOW_DAYS = int(os.environ.get("BC_ORDER_WINDOW_DAYS", "90"))

# Sales document entities that count as "an order", with their date field.
# Linkage: Contact.customerNo -> sellToCustomerNo on each header (see CLAUDE.md).
SALES_DOC_ENTITIES = {
    "SalesInvHeader": "postingDate",   # posted invoices
    "SalesShipHeader": "postingDate",  # shipments
    "SalesHeader": "orderDate",        # open orders (not yet posted orders)
}

_token_cache: dict[str, Any] = {"access_token": None, "expires_at": 0.0}


class BCConfigError(RuntimeError):
    pass


class BCAuthError(RuntimeError):
    pass


def _get_oauth_token() -> str:
    if not (BC_TENANT_ID and BC_CLIENT_ID and BC_CLIENT_SECRET):
        raise BCConfigError("OAuth2 env vars missing")

    now = time.time()
    if _token_cache["access_token"] and _token_cache["expires_at"] > now + 60:
        return _token_cache["access_token"]

    url = f"https://login.microsoftonline.com/{BC_TENANT_ID}/oauth2/v2.0/token"
    resp = requests.post(
        url,
        data={
            "grant_type": "client_credentials",
            "client_id": BC_CLIENT_ID,
            "client_secret": BC_CLIENT_SECRET,
            "scope": BC_OAUTH_SCOPE,
        },
        timeout=15,
    )
    if resp.status_code != 200:
        raise BCAuthError(f"OAuth token request failed: {resp.status_code} {resp.text}")
    payload = resp.json()
    _token_cache["access_token"] = payload["access_token"]
    _token_cache["expires_at"] = now + float(payload.get("expires_in", 3600))
    return _token_cache["access_token"]


def probe_contact() -> bool:
    """Can the Contact entity actually answer a query? Best-effort, never raises.

    Auth alone is not enough to know BC works: on 2026-07-27 the DE-TEST
    sandbox lost ALL custom web-service publications (Contact 404'd for hours)
    while OAuth kept succeeding. This probe is the detection hook — used by
    warmup() at startup and by GET /health/deep for external monitoring."""
    try:
        _odata_get("no ne ''", top=1)
        return True
    except requests.HTTPError as e:
        status = e.response.status_code if e.response is not None else "?"
        if status == 404:
            log.error(
                "BC Contact probe 404 — web service likely UNPUBLISHED "
                "(check the service doc at the ODataV4 root): %s", e
            )
        else:
            log.error("BC Contact probe failed (HTTP %s): %s", status, e)
        return False
    except Exception as e:
        log.error("BC Contact probe failed: %s", e)
        return False


def warmup() -> bool:
    """Pre-fetch the OAuth token AND probe the Contact entity at startup, so the
    first Fonio call doesn't pay the ~2.8 s handshake on the critical path
    (Fonio drops the webhook after 5000 ms) and an unpublished/unreachable
    Contact web service is visible in the startup log instead of surfacing as
    silent no-matches during a live call. Best-effort: returns False (never
    raises) if auth is unconfigured, the handshake fails, or the probe fails.
    """
    if not (BC_TENANT_ID and BC_CLIENT_ID and BC_CLIENT_SECRET):
        return False
    try:
        _get_oauth_token()
    except (BCConfigError, BCAuthError, requests.RequestException):
        return False
    return probe_contact()


def _request_kwargs() -> dict[str, Any]:
    if BC_TENANT_ID and BC_CLIENT_ID and BC_CLIENT_SECRET:
        return {"headers": {"Authorization": f"Bearer {_get_oauth_token()}"}}
    if BC_USERNAME and BC_ACCESS_KEY:
        return {"auth": HTTPBasicAuth(BC_USERNAME, BC_ACCESS_KEY)}
    raise BCConfigError(
        "No auth configured. Set BC_TENANT_ID/BC_CLIENT_ID/BC_CLIENT_SECRET for OAuth2 "
        "or BC_USERNAME/BC_ACCESS_KEY for Basic Auth."
    )


def _digits_only(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def phone_variants(raw: str) -> list[str]:
    """Generate plausible storage formats of the same phone number.

    For DE: +491512222, 00491512222, 491512222, 01512222, 1512222.
    Used to OR exact-match variants on a single field (BC's OData here
    forbids OR across distinct fields, but OR'ing values on one field works).
    """
    digits = _digits_only(raw)
    if not digits:
        return []

    cc = DEFAULT_COUNTRY_CODE
    variants: set[str] = set()

    if digits.startswith("00" + cc):
        national = digits[2 + len(cc):]
    elif digits.startswith(cc) and not raw.lstrip().startswith("0"):
        national = digits[len(cc):]
    elif digits.startswith("0"):
        national = digits[1:]
    else:
        national = digits

    if national:
        variants.add("+" + cc + national)
        variants.add("00" + cc + national)
        variants.add(cc + national)
        variants.add("0" + national)
        variants.add(national)

    variants.add(digits)
    raw_trim = (raw or "").strip()
    if raw_trim:
        variants.add(raw_trim)

    # International twins for foreign callers (e.g. Austria): +43... and
    # 0043... denote the same number; BC may store either form.
    if digits.startswith("00"):
        variants.add("+" + digits[2:])
    if raw_trim.startswith("+"):
        variants.add("00" + digits)

    return sorted(variants)


def _phone_filter_for_field(field: str, variants: list[str]) -> str:
    parts = [f"{field} eq '{_escape(v)}'" for v in variants]
    return " or ".join(parts)


def _select_fields() -> str:
    return ",".join(
        [
            "no",
            "name",
            "firstName",
            "middleName",
            "surname",
            "birthDate",
            "eMail",
            "customerNo",         # -> sales/repair documents (order history)
            "healthInsuranceNo",  # Krankenkasse identifier (reported, not a decision)
            *([BC_POSTAL_FIELD] if BC_POSTAL_FIELD else []),
            *PHONE_FIELDS,
        ]
    )


def _odata_get(filter_expr: str, top: int = 5) -> list[dict[str, Any]]:
    if not BC_CONTACTS_URL:
        raise BCConfigError("BC_CONTACTS_URL is not set")
    params = {
        "$filter": filter_expr,
        "$select": _select_fields(),
        "$top": str(top),
    }
    resp = requests.get(BC_CONTACTS_URL, params=params, timeout=20, **_request_kwargs())
    if resp.status_code == 401:
        raise BCAuthError(f"BC rejected auth: {resp.text}")
    resp.raise_for_status()
    return resp.json().get("value", [])


def _clean_birth_date(value: Any) -> str | None:
    """BC stores an empty birth date as the min-date 0001-01-01 (and some
    company contacts have no real DOB). Treat those as None so the agent never
    reads a bogus date aloud during identity verification."""
    if not value:
        return None
    if str(value).startswith("0001-01-01"):
        return None
    return value


def _shape(contact: dict[str, Any]) -> dict[str, Any]:
    first = contact.get("firstName") or ""
    surname = contact.get("surname") or ""
    display = (f"{first} {surname}").strip() or contact.get("name") or ""
    return {
        "no": contact.get("no"),
        "name": display,
        "first_name": first or None,
        "surname": surname or None,
        "birth_date": _clean_birth_date(contact.get("birthDate")),
        "email": contact.get("eMail"),
        "customer_no": contact.get("customerNo") or None,
        "health_insurance_no": contact.get("healthInsuranceNo") or None,
        "postal_code": (contact.get(BC_POSTAL_FIELD) or None) if BC_POSTAL_FIELD else None,
        # All stored numbers, for identity verification (phone factor) without
        # a second BC round-trip — the fields are already in the $select.
        "phones": [contact.get(f) for f in PHONE_FIELDS if contact.get(f)],
    }


def _escape(value: str) -> str:
    return value.replace("'", "''")


def lookup_by_phone(phone: str) -> dict[str, Any] | None:
    """Find a contact whose stored phone matches `phone` in any phone field.

    BC's OData here rejects OR across different fields, so we query one field
    at a time and stop at the first hit. Within a single field we OR all
    plausible format variants so callers can pass any common format.
    """
    variants = phone_variants(phone)
    if not variants:
        return None

    for field in PHONE_FIELDS:
        filt = _phone_filter_for_field(field, variants)
        rows = _odata_get(filt, top=1)
        if rows:
            return _shape(rows[0])
    return None


def _name_variants(name: str) -> list[str]:
    """German spelling variants for STT robustness: ß↔ss↔s, ä↔ae, ö↔oe, ü↔ue.

    A caller saying "Haußmann" may be transcribed "Haussmann" OR "Hausmann"
    (ß sounds like a plain s); contains() is exact, so we probe the alternates
    too. Some generated forms are nonsense ("ßtraße") — harmless, they just
    return no rows. Returns only variants that differ from the input."""
    to_ascii = (
        name.replace("ß", "ss")
        .replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
        .replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
    )
    to_ascii_single = name.replace("ß", "s")
    to_special = (
        name.replace("ss", "ß")
        .replace("ae", "ä").replace("oe", "ö").replace("ue", "ü")
        .replace("Ae", "Ä").replace("Oe", "Ö").replace("Ue", "Ü")
    )
    # single s -> ß (after ss -> ß so "Haussmann" doesn't become "Haußßmann")
    to_special_single = name.replace("ss", "ß").replace("s", "ß")
    out = []
    for v in (to_ascii, to_ascii_single, to_special, to_special_single):
        if v != name and v not in out:
            out.append(v)
    return out


def _name_probes(cleaned: str) -> list[str]:
    """Probe ladder for a spoken name: full string + spelling variants, then
    the last token (surname) + its variants. Deduplicated, order preserved."""
    probes = [cleaned, *_name_variants(cleaned)]
    tokens = [t for t in re.split(r"\s+", cleaned) if t]
    if len(tokens) >= 2:
        probes += [tokens[-1], *_name_variants(tokens[-1])]
    seen: set[str] = set()
    out = []
    for p in probes:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _probe_filters(probe: str, birth_date_iso: str | None) -> list[str]:
    """$filter expression(s) for one name probe.

    Without a DOB: the plain contains() query (top-N truncation applies — fine
    for candidate lists). With a DOB, TWO separate narrow queries:
      1. contains(name) AND birthDate eq <dob>   — the caller's actual claim;
         its own query so common surnames can never crowd it out of the top-N
         (618 Müllers, and the no-DOB rows sort first by KN-number — verified
         live 2026-07-27 that a combined same-field OR floods the pool).
      2. contains(name) AND birthDate lt 1900-01-01 — records with NO stored
         DOB (BC min-date 0001-01-01), so the no-DOB carve-out keeps working;
         these still need a non-DOB factor to verify.
    """
    base = f"contains(name, '{_escape(probe)}')"
    if not (birth_date_iso and _ISO_DATE_RE.match(birth_date_iso)):
        return [base]
    return [
        f"{base} and birthDate eq {birth_date_iso}",
        f"{base} and birthDate lt 1900-01-01",
    ]


def lookup_by_name(
    name: str, limit: int = 5, birth_date_iso: str | None = None
) -> list[dict[str, Any]]:
    """Find contacts by name using contains() on the `name` field.

    contains() is supported on a single field; only OR across distinct fields
    fails. Retry ladder: full string -> German spelling variants (ß/ss,
    umlauts; STT robustness) -> last token alone (handles "Max Mustermann"
    where only "Mustermann" is stored) -> last-token variants.

    `birth_date_iso` (ISO YYYY-MM-DD, from verification.normalize_dob) narrows
    every probe server-side to that birth date (+ no-DOB records), so the right
    patient is in the result no matter how common the surname.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        return []

    for probe in _name_probes(cleaned):
        rows: list[dict[str, Any]] = []
        seen: set[Any] = set()
        for filt in _probe_filters(probe, birth_date_iso):
            for row in _odata_get(filt, top=limit):
                if row.get("no") not in seen:
                    seen.add(row.get("no"))
                    rows.append(row)
        if rows:
            return [_shape(r) for r in rows]
    return []


def lookup_by_name_all(
    name: str, limit: int = 10, birth_date_iso: str | None = None
) -> list[dict[str, Any]]:
    """Union of matches across ALL spelling probes (no first-hit shortcut).

    lookup_by_name stops at the first probe with rows — fine for candidate
    lists, but fatal for verification when a literal spelling ("Hausmann")
    matches real contacts while the caller is the ß-spelled one ("Haußmann"):
    the right record is never fetched. This variant merges every probe's rows,
    deduped by KN-number, so the factor check sees all plausible spellings.
    Costs up to ~8 sequential queries — use only when the first pass failed."""
    cleaned = (name or "").strip()
    if not cleaned:
        return []
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for probe in _name_probes(cleaned):
        for filt in _probe_filters(probe, birth_date_iso):
            for row in _odata_get(filt, top=limit):
                shaped = _shape(row)
                if shaped["no"] not in seen:
                    seen.add(shaped["no"])
                    out.append(shaped)
        if len(out) >= limit * 3:  # safety cap
            break
    return out


def get_contact_by_no(contact_no: str) -> dict[str, Any] | None:
    """Fetch one contact by its KN-number (the `no` field). Exact eq filter.

    Used by /create-request to re-verify the caller's identity factors against
    BC before creating an order ticket (zero-trust toward the voice agent)."""
    cleaned = (contact_no or "").strip()
    if not cleaned:
        return None
    rows = _odata_get(f"no eq '{_escape(cleaned)}'", top=1)
    return _shape(rows[0]) if rows else None


def get_contacts_by_customer_no(customer_no: str, top: int = 5) -> list[dict[str, Any]]:
    """ALL contacts sharing a caller-spoken Kundennummer (`customerNo`).

    One customer account can carry several contacts — family members, schools,
    carers (verified live 2026-07-28: 4110082 -> 4 contacts). The policy split
    lives in main._resolve_and_verify: exactly one contact -> the number alone
    verifies (2026-07-20); several -> each must pass the full factor rule, so
    a shared number alone never verifies but DOB+Kundennummer disambiguates."""
    cleaned = (customer_no or "").strip()
    # Spoken numbers arrive via STT and may carry grouping separators
    # ("41 42 028", "41-42-028"); BC stores plain digits.
    despaced = re.sub(r"[ .\-/]", "", cleaned)
    if despaced.isdigit():
        cleaned = despaced
    if not cleaned:
        return []
    rows = _odata_get(f"customerNo eq '{_escape(cleaned)}'", top=top)
    return [_shape(r) for r in rows]


def get_contact_by_customer_no(customer_no: str) -> dict[str, Any] | None:
    """The contact for a Kundennummer iff it is unambiguous, else None.
    Kept for diagnostics; verification uses get_contacts_by_customer_no."""
    rows = get_contacts_by_customer_no(customer_no, top=2)
    return rows[0] if len(rows) == 1 else None


def lookup_by_birth_date(birth_date_iso: str, top: int = 25) -> list[dict[str, Any]]:
    """All contacts born on this exact date — the recovery pool for misheard
    names (DOB_POOL_RECOVERY in main.py). Cheap: one query, and only ~2-3
    people share any given date in this tenant (87k contacts / ~36.5k dates)."""
    if not _ISO_DATE_RE.match(birth_date_iso or ""):
        return []
    rows = _odata_get(f"birthDate eq {birth_date_iso}", top=top)
    return [_shape(r) for r in rows]


def _to_national_digits(raw: str) -> str:
    """Reduce a phone number to its national-significant digits.

    Strips separators, then the international/trunk prefixes (`00<cc>`, `<cc>`,
    `0`), so '+49 172 893-4185', '0172 8934185' and '491728934185' all compare
    equal. `<cc>` is only stripped when the raw form isn't trunk-prefixed
    (mirrors phone_variants' handling of e.g. '0491...' Aachen-style numbers).
    """
    digits = _digits_only(raw)
    cc = DEFAULT_COUNTRY_CODE
    if digits.startswith("00" + cc):
        return digits[2 + len(cc):]
    if digits.startswith("00"):
        # Foreign 00-prefixed number (e.g. 0043... for Austria): strip only the
        # international prefix so it compares equal to its +43... twin. The
        # foreign country code stays in — cross-country numbers must not
        # collide with German nationals.
        return digits[2:]
    if digits.startswith(cc) and not (raw or "").lstrip().startswith("0"):
        return digits[len(cc):]
    if digits.startswith("0"):
        return digits[1:]
    return digits


def phone_matches(a: str, b: str) -> bool:
    """True when two phone strings denote the same national number.

    Both sides reduced via _to_national_digits; short strings (<6 digits) never
    match so fragments/extensions can't produce false positives."""
    na, nb = _to_national_digits(a or ""), _to_national_digits(b or "")
    return len(na) >= 6 and na == nb


# --- Spare-part / order eligibility -----------------------------------------
# Linkage runs off Contact.customerNo (NOT the KN-number). See CLAUDE.md.

def _entity_url(entity: str) -> str:
    """Sibling entity set under the same Company('…') OData root as Contact."""
    base = BC_CONTACTS_URL.rsplit("/Contact", 1)[0]
    return f"{base}/{entity}"


def _odata_get_entity(
    entity: str,
    filter_expr: str,
    select: str,
    top: int = 5,
    orderby: str | None = None,
) -> list[dict[str, Any]]:
    params: dict[str, str] = {
        "$filter": filter_expr,
        "$select": select,
        "$top": str(top),
    }
    if orderby:
        params["$orderby"] = orderby
    resp = requests.get(_entity_url(entity), params=params, timeout=20, **_request_kwargs())
    if resp.status_code == 401:
        raise BCAuthError(f"BC rejected auth: {resp.text}")
    resp.raise_for_status()
    return resp.json().get("value", [])


def _has_recent_order(customer_no: str, cutoff: str) -> bool:
    """True if the customer has any sales document on/after `cutoff` (ISO date)."""
    for entity, date_field in SALES_DOC_ENTITIES.items():
        filt = f"sellToCustomerNo eq '{_escape(customer_no)}' and {date_field} ge {cutoff}"
        rows = _odata_get_entity(entity, filt, select=f"no,{date_field}", top=1)
        if rows:
            return True
    return False


def _last_invoice_items(customer_no: str) -> tuple[list[str], str | None]:
    """Most recent posted invoice's line descriptions + its posting date."""
    heads = _odata_get_entity(
        "SalesInvHeader",
        f"sellToCustomerNo eq '{_escape(customer_no)}'",
        select="no,postingDate",
        top=1,
        orderby="postingDate desc",
    )
    if not heads:
        return [], None
    doc_no = heads[0].get("no")
    posting_date = heads[0].get("postingDate")
    lines = _odata_get_entity(
        "SalesInvLine",
        f"documentNo eq '{_escape(doc_no)}'",
        select="documentNo,type,no,description,quantity",
        top=20,
    )
    items = [
        (ln.get("description") or ln.get("no") or "").strip()
        for ln in lines
        if (ln.get("description") or ln.get("no"))
    ]
    return [i for i in items if i], posting_date


# Header entity -> its line entity (where the actual articles live).
SALES_DOC_LINES = {
    "SalesInvHeader": "SalesInvLine",
    "SalesShipHeader": "SalesShipLine",
    "SalesHeader": "SalesLine",
}


def recent_order_for_item(
    customer_no: str,
    item_key: str,
    classify,
    window_days: int = ORDER_WINDOW_DAYS,
) -> dict[str, Any]:
    """ITEM-AWARE recency check for /create-request (policy 2026-07-27): only a
    prior order of the SAME article class blocks — a repair invoice or other
    accessory within the window must not deny e.g. batteries.

    Scans the window's sales documents and classifies each line description
    with `classify` (verification.classify_item, injected to keep this module
    free of app imports — same pattern as phone_matches). Real DE-TEST wording
    verified 2026-07-27: 'Zink Luft Batterien 675 Rayovac (à 6 Stk.)' ->
    BATTERIES; 'Mikrofonabdeckung'/'Microphone Cover left' ->
    MICROPHONE_COVERS; 'Spulenabdeckung'/'Mikrofon Testgerät Kit'/'Coil Cover'
    -> None (correctly non-blocking).

    Returns {"blocked": bool|None, "blocking_date": str|None,
    "blocking_desc": str|None} — blocked None means BC errored / no
    customer_no (caller flags UNGEPRÜFT). Short-circuits on the first match;
    typical case (no documents in window) costs the same 3 header queries as
    the old any-document check."""
    result: dict[str, Any] = {"blocked": None, "blocking_date": None, "blocking_desc": None}
    if not customer_no:
        return result

    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    try:
        for entity, date_field in SALES_DOC_ENTITIES.items():
            heads = _odata_get_entity(
                entity,
                f"sellToCustomerNo eq '{_escape(customer_no)}' and {date_field} ge {cutoff}",
                select=f"no,{date_field}",
                top=5,
                orderby=f"{date_field} desc",
            )
            for head in heads:
                lines = _odata_get_entity(
                    SALES_DOC_LINES[entity],
                    f"documentNo eq '{_escape(head.get('no') or '')}'",
                    select="no,description",
                    top=20,
                )
                for ln in lines:
                    desc = (ln.get("description") or "").strip()
                    if desc and classify(desc) == item_key:
                        result["blocked"] = True
                        result["blocking_date"] = head.get(date_field)
                        result["blocking_desc"] = desc
                        return result
        result["blocked"] = False
    except (requests.RequestException, BCAuthError):
        result["blocked"] = None  # unknown -> caller flags for manual review
    return result


def check_order_eligibility(
    customer_no: str, window_days: int = ORDER_WINDOW_DAYS
) -> dict[str, Any]:
    """Decide whether a customer may order a spare part again.

    RULE (current): 3-month *recency only* — eligible iff no sales document
    (invoice / shipment / open order) exists within `window_days`. The
    Krankenkasse is NOT decided here: BC stores only the insurance number, so
    that half is reported elsewhere and a human makes the coverage call.

    Best-effort: any BC error degrades to permission=None ("unknown") rather
    than raising, so the on-ring lookup never fails the whole call over this.
    Returns: {permission_to_order_again, last_ordered_items, last_order_date}.
    """
    result: dict[str, Any] = {
        "permission_to_order_again": None,
        "last_ordered_items": "",
        "last_order_date": None,
    }
    if not customer_no:
        return result

    cutoff = (date.today() - timedelta(days=window_days)).isoformat()
    try:
        recent = _has_recent_order(customer_no, cutoff)
        result["permission_to_order_again"] = not recent
    except (requests.RequestException, BCAuthError):
        pass  # leave permission as None -> agent treats as "unknown"

    try:
        items, last_date = _last_invoice_items(customer_no)
        result["last_ordered_items"] = ", ".join(items)
        result["last_order_date"] = last_date
    except (requests.RequestException, BCAuthError):
        pass

    return result
