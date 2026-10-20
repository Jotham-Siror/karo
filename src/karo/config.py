"""Settings read from the environment, and the refusal to start without the secrets the app can't run safely without."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from karo import seed

# src/karo/config.py -> repo root. Found from the package, so it works from any working directory.
ENV_FILE = Path(__file__).resolve().parents[2] / ".env"

# Settings field -> the environment variable that holds it, which is what an error message has to name.
_ENV_NAMES = {
    "db_path": "KARO_DB",
    "public_base_url": "PUBLIC_BASE_URL",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "pin_pepper": "KARO_PIN_PEPPER",
    "session_secret": "KARO_SESSION_SECRET",
    "demo_password": "KARO_DEMO_PASSWORD",
    "provider": "KARO_PROVIDER",
    "standin_secret": "KARO_STANDIN_SECRET",
    "demo_pin": "DEMO_PIN",
    "twilio_account_sid": "TWILIO_ACCOUNT_SID",
    "twilio_auth_token": "TWILIO_AUTH_TOKEN",
    "twilio_whatsapp_from": "TWILIO_WHATSAPP_FROM",
}

# The demo members who may be given a real WhatsApp number, each from DEMO_WHATSAPP_<NAME>.
DEMO_WHATSAPP_NAMES = ("amina", "baraka", "nakato")


@dataclass(frozen=True)
class Settings:
    # repr=False on every secret, so a log line or a failed assertion that shows this object leaks nothing.
    db_path: str = "data/karo.db"
    public_base_url: str = "http://localhost:8000"
    anthropic_api_key: str | None = field(default=None, repr=False)
    pin_pepper: str | None = field(default=None, repr=False)
    session_secret: str | None = field(default=None, repr=False)
    demo_password: str | None = field(default=None, repr=False)
    provider: str = "standin"
    standin_secret: str | None = field(default=None, repr=False)
    demo_pin: str | None = field(default=None, repr=False)
    twilio_account_sid: str | None = field(default=None, repr=False)
    twilio_auth_token: str | None = field(default=None, repr=False)
    twilio_whatsapp_from: str | None = field(default=None, repr=False)
    # Real phones for the demo members, by lower-case first name. Personal data, so kept out of the repr like a secret.
    demo_whatsapp: dict[str, str] = field(default_factory=dict, repr=False)

    @property
    def whatsapp_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_whatsapp_from)

    def require(self, *names: str) -> None:
        """Raises naming the environment variables that are unset or empty. Never prints a value."""
        missing = [_ENV_NAMES[name] for name in names if not getattr(self, name)]
        if missing:
            raise RuntimeError(f"Missing settings: {', '.join(missing)}. Set them in .env (see .env.example).")


def _demo_whatsapp(read) -> dict[str, str]:
    numbers = {}
    for name in DEMO_WHATSAPP_NAMES:
        variable = f"DEMO_WHATSAPP_{name.upper()}"
        number = read(variable)
        if number is None:
            continue
        try:
            seed.whatsapp_identities({name: number})
        except ValueError:
            # `from None`: the cause's message quotes the number, and a traceback would print it.
            raise RuntimeError(f"{variable} is not a valid phone number. Write it with its country code.") from None
        numbers[name] = number
    return numbers


def load_settings(env_file: Path | None = ENV_FILE) -> Settings:
    """Reads `env_file` (variables already set in the environment win), then refuses if a secret is missing."""
    if env_file is not None:
        load_dotenv(env_file)

    def read(name: str, default: str | None = None) -> str | None:
        # An empty value (`NAME=` in .env.example) means "not set", so the default applies.
        return os.environ.get(name) or default

    defaults = Settings()
    settings = Settings(
        db_path=read("KARO_DB", defaults.db_path),
        public_base_url=read("PUBLIC_BASE_URL", defaults.public_base_url),
        anthropic_api_key=read("ANTHROPIC_API_KEY"),
        pin_pepper=read("KARO_PIN_PEPPER"),
        session_secret=read("KARO_SESSION_SECRET"),
        demo_password=read("KARO_DEMO_PASSWORD"),
        provider=read("KARO_PROVIDER", defaults.provider),
        standin_secret=read("KARO_STANDIN_SECRET"),
        demo_pin=read("DEMO_PIN"),
        twilio_account_sid=read("TWILIO_ACCOUNT_SID"),
        twilio_auth_token=read("TWILIO_AUTH_TOKEN"),
        twilio_whatsapp_from=read("TWILIO_WHATSAPP_FROM"),
        demo_whatsapp=_demo_whatsapp(read),
    )
    needed = ["pin_pepper", "session_secret", "demo_password"]
    if settings.provider == "standin":
        needed.append("standin_secret")
    settings.require(*needed)
    return settings
