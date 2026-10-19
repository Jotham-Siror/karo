import pytest

from karo.providers.flutterwave import require_test_key


def test_live_secret_key_is_refused():
    # Bug caught: the smoke test or demo running against a live account and moving real money.
    with pytest.raises(RuntimeError, match="test"):
        require_test_key("FLWSECK-abc123-X")


def test_missing_key_is_refused():
    with pytest.raises(RuntimeError):
        require_test_key("")


def test_test_key_is_accepted():
    assert require_test_key("FLWSECK_TEST-abc123-X") == "FLWSECK_TEST-abc123-X"
