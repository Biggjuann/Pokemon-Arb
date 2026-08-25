"""eBay Marketplace Account Deletion notifications.

eBay requires every production keyset to expose an endpoint that receives
account-deletion notices, and refuses to authenticate the keyset until that
endpoint has been validated. The validation is a challenge-response:

    GET  https://<your endpoint>?challenge_code=abc123
    ->   200 application/json  {"challengeResponse": "<sha256 hex>"}

    hash = SHA256(challengeCode + verificationToken + endpoint)

The input order is fixed and the endpoint string must be character-for-
character what is registered in the developer console -- scheme, host, path,
no trailing slash unless registered with one. Getting any of that wrong
produces the same opaque "endpoint validation failed" in the console.

The POST side must acknowledge quickly (200/201/202/204) and actually remove
the user's personal data. The only eBay personal data this app stores is the
seller username on cached listings, so that is what gets scrubbed.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Listing

log = logging.getLogger(__name__)

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,80}$")
TOKEN_MIN, TOKEN_MAX = 32, 80


def compute_challenge_response(challenge_code: str, verification_token: str, endpoint: str) -> str:
    """SHA-256 of challengeCode + verificationToken + endpoint, hex encoded.

    Order is part of the contract -- any other arrangement hashes fine and
    fails validation with no useful message.
    """
    digest = hashlib.sha256()
    digest.update(challenge_code.encode("utf-8"))
    digest.update(verification_token.encode("utf-8"))
    digest.update(endpoint.encode("utf-8"))
    return digest.hexdigest()


def generate_verification_token(length: int = 48) -> str:
    """A token eBay will accept: 32-80 chars of [A-Za-z0-9_-]."""
    length = max(TOKEN_MIN, min(length, TOKEN_MAX))
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def token_problems(token: str | None) -> list[str]:
    """Why eBay would reject this verification token, if it would."""
    if not token:
        return ["not set"]
    problems: list[str] = []
    if len(token) < TOKEN_MIN or len(token) > TOKEN_MAX:
        problems.append(f"must be {TOKEN_MIN}-{TOKEN_MAX} characters, this is {len(token)}")
    if not TOKEN_PATTERN.match(token):
        bad = sorted({c for c in token if not re.match(r"[A-Za-z0-9_-]", c)})
        if bad:
            problems.append(
                "only letters, digits, underscore and hyphen are allowed; "
                f"found {', '.join(repr(c) for c in bad)}"
            )
    return problems


def endpoint_problems(endpoint: str | None) -> list[str]:
    if not endpoint:
        return ["not set"]
    problems: list[str] = []
    if not endpoint.startswith("https://"):
        problems.append("must be an https:// URL")
    if endpoint != endpoint.strip():
        problems.append("has surrounding whitespace")
    if endpoint.endswith("/"):
        problems.append("ends with a trailing slash -- it must match the console entry exactly")
    return problems


def purge_user_data(session: Session, username: str | None) -> int:
    """Remove the deleted user's personal data from cached listings.

    Their listings stay (the card and price are not personal data) but the
    username is cleared, which is the only eBay personal data held here.
    """
    if not username:
        return 0
    affected = list(session.scalars(select(Listing).where(Listing.seller_username == username)))
    for listing in affected:
        listing.seller_username = None
    return len(affected)


def extract_username(payload: dict[str, Any]) -> str | None:
    """Pull the username out of an account-deletion notification body."""
    data = (payload or {}).get("notification", {}).get("data", {})
    if not isinstance(data, dict):
        return None
    username = data.get("username")
    return username if isinstance(username, str) and username else None
