"""Settings: what the app reads from the environment and won't start without. Every test names its bug."""

from pathlib import Path

import pytest

from karo.config import Settings, load_settings

ALL_NAMES = (
    "KARO_DB",
    "PUBLIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "KARO_PIN_PEPPER",
    "KARO_SESSION_SECRET",
    "KARO_DEMO_PASSWORD",
    "KARO_PROVIDER",
    "KARO_STANDIN_SECRET",
    "DEMO_PIN",
    "TWILIO_ACCOUNT_SID",
    "TWILIO_AUTH_TOKEN",
    "TWILIO_WHATSAPP_FROM",
    "DEMO_WHATSAPP_AMINA",
    "DEMO_WHATSAPP_BARAKA",
    "DEMO_WHATSAPP_NAKATO",
)
REQUIRED = {
    "KARO_PIN_PEPPER": "pepper-value-xyz",
    "KARO_SESSION_SECRET": "session-value-xyz",
    "KARO_DEMO_PASSWORD": "password-value-xyz",
    "KARO_STANDIN_SECRET": "standin-value-xyz",
}
NO_ENV_FILE = Path("no-such-file.env")


@pytest.fixture
def env(monkeypatch):
    """An environment with none of Karo's variables set, restored after the test even if the loader sets some."""
    for name in ALL_NAMES:
        monkeypatch.setenv(name, "placeholder")
        monkeypatch.delenv(name)
    return monkeypatch


def set_required(env, **overrides):
    for name, value in {**REQUIRED, **overrides}.items():
        env.setenv(name, value)


def test_missing_settings_are_named_and_their_values_are_not(env):
    # Catches: a start-up error that hides which variable is missing, or prints the ones that are set.
    env.setenv("KARO_PIN_PEPPER", REQUIRED["KARO_PIN_PEPPER"])
    with pytest.raises(RuntimeError) as caught:
        load_settings(NO_ENV_FILE)
    message = str(caught.value)
    for name in ("KARO_SESSION_SECRET", "KARO_DEMO_PASSWORD", "KARO_STANDIN_SECRET"):
        assert name in message
    assert "KARO_PIN_PEPPER" not in message
    assert REQUIRED["KARO_PIN_PEPPER"] not in message


@pytest.mark.parametrize("name", list(REQUIRED))
def test_an_empty_secret_counts_as_missing(env, name):
    # Catches: an empty pepper or session secret passing as "set", which makes PINs and cookies guessable.
    set_required(env)
    env.setenv(name, "")
    with pytest.raises(RuntimeError, match=name):
        load_settings(NO_ENV_FILE)


def test_the_standin_secret_is_only_needed_for_the_standin_provider(env):
    # Catches: a real-provider setup being refused for a stand-in secret it has no use for.
    set_required(env, KARO_PROVIDER="flutterwave")
    env.delenv("KARO_STANDIN_SECRET")
    assert load_settings(NO_ENV_FILE).provider == "flutterwave"


def test_the_anthropic_key_is_not_needed_to_load_settings(env):
    # Catches: tests and the stand-in demo being unable to start without a real API key.
    set_required(env)
    assert load_settings(NO_ENV_FILE).anthropic_api_key is None


def test_defaults_apply_when_a_value_is_unset_or_empty(env):
    # Catches: an empty `PUBLIC_BASE_URL=` line in .env producing confirm links with no host in them.
    set_required(env)
    env.setenv("PUBLIC_BASE_URL", "")
    settings = load_settings(NO_ENV_FILE)
    assert settings.public_base_url == "http://localhost:8000"
    assert settings.db_path == "data/karo.db"
    assert settings.provider == "standin"


def test_values_come_from_the_env_file(env, tmp_path):
    # Catches: the .env file never being read, so a plain `uvicorn` start has no secrets.
    env_file = tmp_path / ".env"
    env_file.write_text(
        "".join(f"{name}={value}\n" for name, value in REQUIRED.items()) + "DEMO_PIN=4821\nKARO_DB=elsewhere/karo.db\n"
    )
    settings = load_settings(env_file)
    assert (settings.pin_pepper, settings.demo_pin, settings.db_path) == (
        REQUIRED["KARO_PIN_PEPPER"],
        "4821",
        "elsewhere/karo.db",
    )


def test_require_names_the_variable_not_the_field_or_the_value():
    # Catches: asking for a missing key and getting a Python field name, or the value of a different secret.
    settings = Settings(pin_pepper="pepper-value-xyz")
    with pytest.raises(RuntimeError) as caught:
        settings.require("pin_pepper", "anthropic_api_key", "demo_pin")
    message = str(caught.value)
    assert "ANTHROPIC_API_KEY" in message and "DEMO_PIN" in message
    assert "KARO_PIN_PEPPER" not in message and "pepper-value-xyz" not in message


def test_a_settings_object_does_not_print_its_secrets():
    # Catches: a log line or a failed assertion showing the settings object and leaking every key in it.
    settings = Settings(
        anthropic_api_key="key-value-xyz",
        pin_pepper="pepper-value-xyz",
        session_secret="session-value-xyz",
        demo_password="password-value-xyz",
        standin_secret="standin-value-xyz",
        demo_pin="4821",
    )
    shown = repr(settings)
    for secret in ("key-value-xyz", "pepper-value-xyz", "session-value-xyz", "password-value-xyz", "standin-value-xyz"):
        assert secret not in shown
    assert "4821" not in shown


# --- WhatsApp -----------------------------------------------------------------------------------


def test_the_twilio_settings_are_read_and_the_door_is_off_without_all_three(env):
    # Catches: a half-set Twilio setup switching the WhatsApp door on, with a webhook that can never check a signature.
    set_required(env)
    assert load_settings(NO_ENV_FILE).whatsapp_enabled is False

    env.setenv("TWILIO_ACCOUNT_SID", "ACsid-value-xyz")
    env.setenv("TWILIO_AUTH_TOKEN", "token-value-xyz")
    assert load_settings(NO_ENV_FILE).whatsapp_enabled is False

    env.setenv("TWILIO_WHATSAPP_FROM", "whatsapp:+14155550100")
    settings = load_settings(NO_ENV_FILE)
    assert settings.whatsapp_enabled is True
    assert (settings.twilio_account_sid, settings.twilio_auth_token, settings.twilio_whatsapp_from) == (
        "ACsid-value-xyz",
        "token-value-xyz",
        "whatsapp:+14155550100",
    )


def test_demo_phones_are_kept_and_empty_ones_are_skipped(env):
    # Catches: an empty `DEMO_WHATSAPP_BARAKA=` line seeding a phone for Baraka, or the set ones being dropped.
    set_required(env)
    env.setenv("DEMO_WHATSAPP_AMINA", "+254700000101")
    env.setenv("DEMO_WHATSAPP_BARAKA", "")
    env.setenv("DEMO_WHATSAPP_NAKATO", "0770 000 103")
    assert load_settings(NO_ENV_FILE).demo_whatsapp == {"amina": "+254700000101", "nakato": "0770 000 103"}


@pytest.mark.parametrize("name", ["DEMO_WHATSAPP_AMINA", "DEMO_WHATSAPP_BARAKA", "DEMO_WHATSAPP_NAKATO"])
def test_a_bad_demo_phone_is_a_start_up_error_that_names_the_variable_and_not_the_number(env, name):
    # Catches: a typo in a phone number found only when a message never arrives, or the number printed into a log.
    set_required(env)
    env.setenv(name, "12-not-a-phone-345")
    with pytest.raises(RuntimeError) as caught:
        load_settings(NO_ENV_FILE)
    assert name in str(caught.value)
    assert "12-not-a-phone-345" not in str(caught.value) and "12-not-a-phone-345" not in repr(caught.value.__cause__)
    assert caught.value.__suppress_context__ is True  # so the traceback does not print the number from the cause


def test_the_twilio_secrets_and_demo_phones_are_not_in_the_settings_repr():
    # Catches: a log line or failed assertion showing the settings object and leaking the Twilio token or a phone.
    settings = Settings(
        twilio_account_sid="ACsid-value-xyz",
        twilio_auth_token="token-value-xyz",
        twilio_whatsapp_from="whatsapp:+14155550100",
        demo_whatsapp={"amina": "+254700000101"},
    )
    shown = repr(settings)
    for secret in ("ACsid-value-xyz", "token-value-xyz", "+14155550100", "+254700000101"):
        assert secret not in shown
