"""Country settings, read from config/countries.json. A new country is a new entry there, not a code change."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import phonenumbers

# src/karo/countries.py -> repo root. Found from the package, so it works from any working directory.
_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"


@dataclass(frozen=True)
class CollectMethod:
    id: str
    label: str
    flutterwave_option: str


@dataclass(frozen=True)
class PayoutMethod:
    """A way to send money into a country, such as mobile money or a bank account."""

    id: str
    label: str


@dataclass(frozen=True)
class Country:
    name: str
    currency: str
    timezone: str
    phone_region: str
    collect_methods: tuple[CollectMethod, ...]
    payout_fields: tuple[str, ...]
    payout_methods: tuple[PayoutMethod, ...] = ()


def load(path: str | Path | None = None) -> dict[str, Country]:
    source = Path(path) if path is not None else _CONFIG_DIR / "countries.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    return {
        code: Country(
            name=entry["name"],
            currency=entry["currency"],
            timezone=entry["timezone"],
            phone_region=entry["phone_region"],
            collect_methods=tuple(
                CollectMethod(m["id"], m["label"], m["flutterwave_option"]) for m in entry["collect_methods"]
            ),
            payout_fields=tuple(entry["payout_fields"]),
            payout_methods=tuple(PayoutMethod(m["id"], m["label"]) for m in entry.get("payout_methods", [])),
        )
        for code, entry in raw.items()
    }


def get(code: str) -> Country:
    known = load()
    if code not in known:
        raise KeyError(f"Karo has no settings for country {code!r}. Known countries: {', '.join(sorted(known))}.")
    return known[code]


def normalise_phone(raw: str, country_code: str) -> str:
    """E.164 form of a phone number written the way people write it in `country_code`."""
    country = get(country_code)
    try:
        number = phonenumbers.parse(raw, country.phone_region)
    except phonenumbers.NumberParseException as exc:
        raise ValueError(f"{raw!r} isn't a phone number.") from exc
    if not phonenumbers.is_valid_number(number):
        raise ValueError(f"{raw!r} isn't a valid {country.name} phone number.")
    return phonenumbers.format_number(number, phonenumbers.PhoneNumberFormat.E164)


def phone_is_in(e164: str, country_code: str) -> bool:
    """True if a number in international form belongs to `country_code`. normalise_phone accepts any valid number."""
    return phonenumbers.region_code_for_number(phonenumbers.parse(e164)) == get(country_code).phone_region


def institutions(path: str | Path | None = None) -> list[dict]:
    source = Path(path) if path is not None else _CONFIG_DIR / "institutions.json"
    return json.loads(source.read_text(encoding="utf-8"))
