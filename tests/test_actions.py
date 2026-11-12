import json

import pytest
import respx

from bridge.spoolman import describe, spool_label
from bridge.sync import grams_text
from tests.test_bridge import SPOOLMAN, existing_spool, linked_extra, make_bridge, mock_deduction, mock_fields, printer


def named(spool):
    return {**spool, "filament": {"name": "PETG-GF Black", "vendor": {"name": "ELEGOO"}}}


def test_spool_label_uses_the_vendor_and_filament_name():
    assert spool_label(named({"id": 55})) == "#55 (ELEGOO PETG-GF Black)"
    assert spool_label({"id": 55}) == "#55"


@pytest.mark.parametrize("value,text", [(0.04, "less than 0.1 g"), (0.4, "0.4 g"), (1.44, "1.4 g"), (6.0, "6 g"), (12.4, "12 g")])
def test_grams_text(value, text):
    assert grams_text(value) == text


def test_describe_is_a_fallback_in_plain_words():
    assert describe("PATCH", "/spool/52") == "Updated spool #52 in Spoolman"
    assert describe("POST", "/vendor") == "Created vendor in Spoolman"


@respx.mock(assert_all_called=False)
def test_a_deduction_is_described_in_plain_words(respx_mock, tmp_path):
    mock_fields(respx_mock)
    mock_deduction(respx_mock, named(existing_spool(extra=linked_extra(10))))
    bridge = make_bridge(tmp_path, printer(consumed=16))
    bridge.poll()
    action = bridge.bridge.spoolman.actions[-1]
    assert action["summary"] == "Deducted 6 g from spool #42 (ELEGOO PETG-GF Black)"
    assert action["text"].startswith("PATCH /spool/42 ")
    assert action["dry_run"] is False


@respx.mock(assert_all_called=False)
def test_a_dry_run_baseline_is_described_too(respx_mock, tmp_path):
    mock_fields(respx_mock)
    mock_deduction(respx_mock, existing_spool(extra=linked_extra(None)))
    bridge = make_bridge(tmp_path, printer(consumed=40), dry_run=True)
    bridge.poll()
    summaries = [a["summary"] for a in bridge.bridge.spoolman.actions]
    assert "Started counting consumption on spool #42 at 40 g" in summaries
    assert all(a["dry_run"] for a in bridge.bridge.spoolman.actions)


@respx.mock(assert_all_called=False)
def test_a_failed_write_is_not_listed_as_done(respx_mock, tmp_path):
    from bridge.spoolman import SpoolmanError
    mock_fields(respx_mock)
    spool = named(existing_spool())
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/42").respond(json=spool)
    respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/42").mock(side_effect=[respx.MockResponse(422), respx.MockResponse(200, json={})])
    bridge = make_bridge(tmp_path, printer()).bridge
    bridge.spoolman.captured.clear()
    with pytest.raises(SpoolmanError):
        bridge.spoolman.update_spool(42, {"extra": {}}, "First try")
    bridge.spoolman.update_spool(42, {"extra": {}}, "Second try")
    assert [a["summary"] for a in bridge.spoolman.captured] == ["Second try"]
