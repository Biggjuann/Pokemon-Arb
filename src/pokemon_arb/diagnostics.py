"""Answer "why is eBay rejecting us" without a shell or a secret leak.

A 401 invalid_client at the token endpoint has a small set of causes, and
they are hard to tell apart by staring at Railway variables. eBay issues
three values per production keyset -- App ID, Dev ID and Cert ID -- and only
App ID + Cert ID are the OAuth pair. Using the Dev ID as the secret, or
pasting a value with a stray newline, produces exactly this error.

Every check reports a *fingerprint* of a credential, never the credential.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any

from .config import Settings

# Production App IDs carry "-PRD-", sandbox ones "-SBX-".
_APP_ID_ENV = re.compile(r"-(PRD|SBX)-", re.IGNORECASE)
# A Dev ID is a bare UUID. A Cert ID starts PRD-/SBX- and is not a bare UUID.
_BARE_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


@dataclass
class Check:
    name: str
    ok: bool | None  # None = could not run
    detail: str
    fix: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, name: str, ok: bool | None, detail: str, fix: str = "") -> None:
        self.checks.append(Check(name=name, ok=ok, detail=detail, fix=fix))

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks if c.ok is not None)

    @property
    def first_failure(self) -> Check | None:
        return next((c for c in self.checks if c.ok is False), None)


def fingerprint(value: str | None) -> str:
    """Enough to compare against the eBay console, not enough to use."""
    if not value:
        return "not set"
    stripped = value.strip().strip("\"'")
    shape = f"{len(stripped)} chars, {stripped[:4]}...{stripped[-2:]}"
    notes = []
    if value != value.strip():
        notes.append("HAS SURROUNDING WHITESPACE")
    if value.strip() != stripped:
        notes.append("WRAPPED IN QUOTES")
    return shape + (f" [{', '.join(notes)}]" if notes else "")


def _raw_env(name: str) -> str | None:
    return os.environ.get(name)


def inspect_credentials(settings: Settings, report: Report) -> None:
    """Static checks -- shape problems that explain invalid_client."""
    raw_id = _raw_env("EBAY_CLIENT_ID")
    raw_secret = _raw_env("EBAY_CLIENT_SECRET")

    if not (settings.ebay_client_id and settings.ebay_client_secret):
        report.add(
            "Credentials present",
            False,
            f"EBAY_CLIENT_ID: {fingerprint(raw_id)} / EBAY_CLIENT_SECRET: {fingerprint(raw_secret)}",
            "Set both variables. With either missing the app silently stays in demo mode.",
        )
        return

    report.add(
        "Credentials present",
        True,
        f"EBAY_CLIENT_ID: {fingerprint(raw_id)}  |  EBAY_CLIENT_SECRET: {fingerprint(raw_secret)}",
    )

    # Whitespace or quotes survive a copy-paste and break Basic auth silently.
    dirty = [
        name
        for name, raw in (("EBAY_CLIENT_ID", raw_id), ("EBAY_CLIENT_SECRET", raw_secret))
        if raw and (raw != raw.strip() or raw.strip() != raw.strip("\"'"))
    ]
    report.add(
        "Credentials are clean",
        not dirty,
        "no stray whitespace or quotes" if not dirty else f"{', '.join(dirty)} needs trimming",
        "Re-paste the value with no trailing newline, space or quote marks." if dirty else "",
    )

    client_id = settings.ebay_client_id
    secret = settings.ebay_client_secret

    # App ID environment vs EBAY_ENV.
    match = _APP_ID_ENV.search(client_id)
    if match:
        keyset_env = "sandbox" if match.group(1).upper() == "SBX" else "production"
        agrees = keyset_env == settings.ebay_env
        report.add(
            "Keyset matches EBAY_ENV",
            agrees,
            f"App ID looks like a {keyset_env} keyset; EBAY_ENV={settings.ebay_env}",
            "" if agrees else f"Set EBAY_ENV={keyset_env}, or use a {settings.ebay_env} keyset.",
        )
    else:
        report.add(
            "Keyset matches EBAY_ENV",
            None,
            "App ID has no -PRD-/-SBX- marker, cannot tell which keyset this is",
        )

    # A Cert ID carries its own PRD-/SBX- prefix. App ID and Cert ID taken
    # from different keysets is a mix-up the individual shape checks miss.
    secret_env = None
    if secret[:4].upper() in ("PRD-", "SBX-"):
        secret_env = "production" if secret[:4].upper() == "PRD-" else "sandbox"
    if secret_env and match:
        app_env = "sandbox" if match.group(1).upper() == "SBX" else "production"
        agree = secret_env == app_env
        report.add(
            "App ID and Cert ID are the same keyset",
            agree,
            f"App ID is {app_env}, Cert ID is {secret_env}",
            ""
            if agree
            else "These are from different keysets. Copy both values from the same "
            "application in the developer console.",
        )

    # The classic: Dev ID pasted where the Cert ID belongs.
    looks_like_dev_id = bool(_BARE_UUID.match(secret))
    report.add(
        "Secret is the Cert ID",
        not looks_like_dev_id,
        "secret is a bare UUID, which is the shape of a Dev ID -- not a Cert ID"
        if looks_like_dev_id
        else "secret does not look like a Dev ID",
        "eBay issues App ID, Dev ID and Cert ID. The OAuth pair is App ID + "
        "Cert ID; the Dev ID is not the client secret."
        if looks_like_dev_id
        else "",
    )


def check_ebay(settings: Settings, *, live: bool = True) -> Report:
    """Static credential checks, then a real token request and search."""
    report = Report()
    report.add("Endpoint", True, f"{settings.ebay_api_base} (EBAY_ENV={settings.ebay_env})")
    inspect_credentials(settings, report)

    if not live or not settings.has_ebay_credentials:
        return report

    from .sources.ebay import EbayClient, EbayError

    client = EbayClient(
        settings.ebay_client_id,
        settings.ebay_client_secret,
        api_base=settings.ebay_api_base,
        marketplace=settings.ebay_marketplace,
    )
    try:
        try:
            client.token()
        except EbayError as exc:
            report.add(
                "OAuth token",
                False,
                str(exc)[:400],
                "invalid_client means eBay rejected the App ID / Cert ID pair itself. "
                "If the shape checks above all pass, the values are well formed and "
                "the problem is the keyset itself -- most often it exists in the "
                "console but is not enabled for production. Reproduce it outside "
                "this app to confirm: curl -X POST "
                f"{settings.ebay_api_base}/identity/v1/oauth2/token "
                "-u 'APP_ID:CERT_ID' -d "
                "'grant_type=client_credentials&scope=https://api.ebay.com/oauth/api_scope'",
            )
            return report
        report.add("OAuth token", True, "eBay issued an application access token")

        # Unfiltered first: separates "auth works but our filters are wrong"
        # from "auth works and the market is simply quiet".
        try:
            plain = list(client.search("charizard", limit=1, filter_string=None, sort=None))
            report.add("Search (no filters)", True, f"returned {len(plain)} item(s)")
        except EbayError as exc:
            report.add(
                "Search (no filters)",
                False,
                str(exc)[:400],
                "Token works but search does not -- usually the keyset lacks Browse API access.",
            )
            return report

        try:
            filtered = list(
                client.search(
                    "charizard base set 4",
                    category_ids=settings.ebay_category_id,
                    limit=5,
                    filter_string=EbayClient.build_filter(
                        max_price_cents=100_000,
                        min_price_cents=100,
                        item_location_country=settings.ebay_delivery_country,
                        delivery_country=settings.ebay_delivery_country,
                    ),
                    sort="price",
                )
            )
            report.add(
                "Search (scanner's filters)",
                True,
                f"returned {len(filtered)} item(s) in category {settings.ebay_category_id}",
            )
        except EbayError as exc:
            report.add(
                "Search (scanner's filters)",
                False,
                str(exc)[:400],
                "Plain search works, so the category id or filter string is the problem.",
            )
    finally:
        client.close()
    return report


def check_pricecharting(settings: Settings, *, live: bool = True) -> Report:
    report = Report()
    if settings.uses_peer_comps:
        report.add(
            "PriceCharting",
            None,
            "not used: COMP_SOURCE=peer values listings against other live eBay asks",
        )
        return report
    if not settings.pricecharting_token:
        report.add(
            "PriceCharting token",
            False,
            "PRICECHARTING_TOKEN is not set",
            "Comps cannot be synced without it.",
        )
        return report
    report.add("PriceCharting token", True, fingerprint(_raw_env("PRICECHARTING_TOKEN")))
    if not live:
        return report

    from .sources.pricecharting import PriceChartingClient, PriceChartingError

    try:
        with PriceChartingClient(settings.pricecharting_token) as client:
            found = client.search_products("charizard")
        report.add("PriceCharting lookup", True, f"returned {len(found)} product(s)")
    except PriceChartingError as exc:
        report.add("PriceCharting lookup", False, str(exc)[:300])
    except Exception as exc:  # network, TLS, etc
        report.add("PriceCharting lookup", False, f"{type(exc).__name__}: {exc}"[:300])
    return report


def check_account_deletion(settings: Settings, *, live: bool = True) -> Report:
    """eBay validates this endpoint before a production keyset authenticates.

    The live check calls our own public URL with a random challenge and
    verifies the reply, which is exactly what eBay does on Save -- so a pass
    here means the console will accept it.
    """
    import secrets

    from .ebay_notifications import (
        compute_challenge_response,
        endpoint_problems,
        token_problems,
    )

    report = Report()
    token = settings.ebay_verification_token
    endpoint = settings.ebay_deletion_endpoint_url

    problems = token_problems(token)
    report.add(
        "Verification token",
        not problems,
        fingerprint(token) if token else "not set",
        "Set EBAY_VERIFICATION_TOKEN. Generate one with `pokearb ebay-token`. "
        + "; ".join(problems)
        if problems
        else "",
    )

    problems = endpoint_problems(endpoint)
    report.add(
        "Endpoint URL",
        not problems,
        endpoint or "not set",
        "Set EBAY_DELETION_ENDPOINT_URL to the exact URL registered in the eBay "
        "console -- it is hashed into the response, so any difference fails. " + "; ".join(problems)
        if problems
        else "",
    )

    if not (token and endpoint) or not live:
        return report

    # Reproduce eBay's own validation call against the public URL.
    import httpx

    challenge = secrets.token_hex(8)
    expected = compute_challenge_response(challenge, token, endpoint)
    try:
        response = httpx.get(endpoint, params={"challenge_code": challenge}, timeout=15.0)
    except Exception as exc:
        report.add(
            "Endpoint answers eBay's challenge",
            False,
            f"could not reach {endpoint}: {type(exc).__name__}: {exc}"[:300],
            "eBay must be able to reach this URL over HTTPS from the public internet.",
        )
        return report

    if response.status_code != 200:
        report.add(
            "Endpoint answers eBay's challenge",
            False,
            f"HTTP {response.status_code}: {response.text[:200]}",
            "The challenge must return 200 with a JSON challengeResponse.",
        )
        return report

    try:
        got = response.json().get("challengeResponse")
    except ValueError:
        got = None
    matches = got == expected
    report.add(
        "Endpoint answers eBay's challenge",
        matches,
        "challenge response matches"
        if matches
        else f"expected {expected[:16]}..., endpoint returned {str(got)[:16]}...",
        ""
        if matches
        else "Usually EBAY_DELETION_ENDPOINT_URL does not match the URL eBay is "
        "calling, character for character.",
    )
    content_type = response.headers.get("content-type", "")
    report.add(
        "Challenge content type",
        content_type.startswith("application/json"),
        content_type or "missing",
        "" if content_type.startswith("application/json") else "eBay requires application/json.",
    )
    return report


def full_report(settings: Settings, *, live: bool = True) -> dict[str, Any]:
    return {
        "ebay": check_ebay(settings, live=live),
        "pricecharting": check_pricecharting(settings, live=live),
    }
