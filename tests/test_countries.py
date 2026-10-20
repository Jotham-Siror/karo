"""Country settings. Adding a country must be a data change, so these tests read the settings rather than the code."""

import json

import pytest

from karo import countries


def test_the_default_settings_hold_kenya_uganda_britain_and_cameroon():
    # Bug caught: a country dropped from, or misspelt in, config/countries.json.
    loaded = countries.load()
    assert set(loaded) == {"KE", "UG", "GB", "CM"}
    assert (loaded["KE"].currency, loaded["KE"].timezone) == ("KES", "Africa/Nairobi")
    assert (loaded["UG"].currency, loaded["UG"].timezone) == ("UGX", "Africa/Kampala")
    assert (loaded["GB"].currency, loaded["GB"].timezone) == ("GBP", "Europe/London")


def test_the_default_path_does_not_depend_on_the_working_directory(tmp_path, monkeypatch):
    # Bug caught: a path relative to the current directory, which breaks as soon as the server starts from elsewhere.
    monkeypatch.chdir(tmp_path)
    assert "KE" in countries.load()
    assert countries.institutions()


def test_uganda_collects_by_mtn_mobile_money_and_card_from_settings():
    # Bug caught: collect methods hard-coded per country instead of read from the settings file.
    uganda = countries.get("UG")
    assert [(m.id, m.label, m.flutterwave_option) for m in uganda.collect_methods] == [
        ("mtn_ug", "MTN Mobile Money", "mobilemoneyuganda"),
        ("card", "Card", "card"),
    ]


def test_kenya_collects_by_mpesa_and_card_and_britain_by_card_only():
    # Bug caught: a method offered in a country that doesn't have it (M-Pesa in the UK).
    assert [m.id for m in countries.get("KE").collect_methods] == ["mpesa", "card"]
    assert [m.id for m in countries.get("GB").collect_methods] == ["card"]


def test_payout_fields_differ_by_country():
    # Bug caught: asking a UK university for a phone number, or a student in Kenya for a sort code.
    assert countries.get("KE").payout_fields == ("phone",)
    assert countries.get("GB").payout_fields == ("sort_code", "account_number")


def test_a_made_up_country_in_another_file_works_with_no_code_change(tmp_path):
    # Bug caught: a country list or if/elif in code, so a new country needs a code change.
    path = tmp_path / "countries.json"
    path.write_text(
        json.dumps(
            {
                "TZ": {
                    "name": "Tanzania",
                    "currency": "TZS",
                    "timezone": "Africa/Dar_es_Salaam",
                    "phone_region": "TZ",
                    "collect_methods": [
                        {"id": "vodacom_tz", "label": "M-Pesa Tanzania", "flutterwave_option": "mpesa"}
                    ],
                    "payout_fields": ["phone"],
                }
            }
        ),
        encoding="utf-8",
    )
    tanzania = countries.load(path)["TZ"]
    assert tanzania.currency == "TZS"
    assert [m.id for m in tanzania.collect_methods] == ["vodacom_tz"]
    assert tanzania.payout_fields == ("phone",)


def test_an_unknown_country_raises_a_plain_key_error():
    # Bug caught: an unknown code returning None and failing later, far from the cause.
    with pytest.raises(KeyError) as caught:
        countries.get("ZZ")
    assert "ZZ" in caught.value.args[0]


def test_a_kenyan_number_is_normalised_to_e164():
    # Bug caught: a local number with spaces and a leading zero never matching the sender's WhatsApp number.
    assert countries.normalise_phone("0712 345 678", "KE") == "+254712345678"


def test_a_ugandan_number_is_normalised_to_e164():
    # Bug caught: using Kenya's dialling code for every number.
    assert countries.normalise_phone("0772 123 456", "UG") == "+256772123456"


def test_a_number_already_in_international_form_is_kept():
    # Bug caught: a "+" number being re-prefixed with the country code.
    assert countries.normalise_phone("+256 772 123 456", "KE") == "+256772123456"


@pytest.mark.parametrize("raw", ["", "abc", "0712", "0712 345 678 999"])
def test_an_invalid_number_raises_value_error(raw):
    # Bug caught: garbage or a short number being saved as a member's phone and then silently never matching anyone.
    with pytest.raises(ValueError):
        countries.normalise_phone(raw, "KE")


def test_a_phone_for_an_unknown_country_raises():
    # Bug caught: falling back to some default region and accepting a number for the wrong country.
    with pytest.raises(KeyError):
        countries.normalise_phone("0712 345 678", "ZZ")


def test_every_institution_is_marked_as_test_data():
    # Bug caught: a made-up university losing its test_data flag and being mistaken for a real payee.
    entries = countries.institutions()
    assert {e["name"] for e in entries} == {"University of Westbridge", "Northvale College"}
    assert all(e["test_data"] is True for e in entries)


def test_westbridge_details_are_the_ones_the_rules_tests_trust():
    # Bug caught: the seeded trusted payee not matching the invoice fixtures, so every demo payment is blocked.
    westbridge = next(e for e in countries.institutions() if e["name"] == "University of Westbridge")
    assert (westbridge["country"], westbridge["currency"]) == ("GB", "GBP")
    assert (westbridge["sort_code"], westbridge["account_number"]) == ("20-40-60", "41234567")


def test_cameroon_is_sendable_from_settings_alone():
    # Bug caught: Cameroon needing code (a currency or phone region written in Python) instead of a settings entry.
    cameroon = countries.get("CM")
    assert (cameroon.name, cameroon.currency) == ("Cameroon", "XAF")
    assert (cameroon.phone_region, cameroon.timezone) == ("CM", "Africa/Douala")
    assert cameroon.payout_fields == ("phone",)
    assert [(m.id, m.label) for m in cameroon.payout_methods] == [
        ("mtn_cm", "MTN Mobile Money"),
        ("orange_cm", "Orange Money"),
    ]


def test_payout_methods_differ_by_country():
    # Bug caught: offering M-Pesa for a bank in Britain, or a bank transfer where only mobile money works.
    assert [m.id for m in countries.get("KE").payout_methods] == ["mpesa"]
    assert [m.id for m in countries.get("UG").payout_methods] == ["mtn_ug", "airtel_ug"]
    assert [m.id for m in countries.get("GB").payout_methods] == ["bank"]


def test_a_country_with_no_payout_methods_listed_still_loads(tmp_path):
    # Bug caught: the new payout list being required, so every older settings file stops loading.
    path = tmp_path / "countries.json"
    path.write_text(
        json.dumps(
            {
                "TZ": {
                    "name": "Tanzania",
                    "currency": "TZS",
                    "timezone": "Africa/Dar_es_Salaam",
                    "phone_region": "TZ",
                    "collect_methods": [],
                    "payout_fields": ["phone"],
                }
            }
        ),
        encoding="utf-8",
    )
    assert countries.load(path)["TZ"].payout_methods == ()


def test_a_cameroon_number_is_normalised_to_e164():
    # Bug caught: a Cameroon number being read with another country's dialling code.
    assert countries.normalise_phone("6 71 23 45 67", "CM") == "+237671234567"
