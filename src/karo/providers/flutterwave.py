"""Flutterwave v3, test mode only. The full provider adapter will live here."""

from __future__ import annotations

BASE_URL = "https://api.flutterwave.com/v3"
TEST_KEY_PREFIX = "FLWSECK_TEST-"


def require_test_key(secret_key: str) -> str:
    """Karo's entry only ever moves test-mode money. Refuse anything that isn't a test key."""
    if not secret_key:
        raise RuntimeError("FLW_SECRET_KEY is not set. Add your Flutterwave test secret key to .env.")
    if not secret_key.startswith(TEST_KEY_PREFIX):
        raise RuntimeError(f"Refusing to run: FLW_SECRET_KEY is not a test key (must start {TEST_KEY_PREFIX}).")
    return secret_key
