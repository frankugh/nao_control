import pytest

from dialog.interfaces import parse_cmdrec_bundle, parse_confirm_method


@pytest.mark.parametrize(
    "value,expected",
    [
        ("none", "none"),
        ("latest", "latest"),
        ("v15", "v15"),
    ],
)
def test_parse_cmdrec_bundle_accepts_expected_values(value, expected):
    assert parse_cmdrec_bundle(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [
        ("none", "none"),
        ("head", "head"),
        ("enter", "enter"),
        ("popup", "popup"),
        ("web", "web"),
    ],
)
def test_parse_confirm_method_accepts_expected_values(value, expected):
    assert parse_confirm_method(value) == expected
