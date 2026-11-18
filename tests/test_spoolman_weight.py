import json

import pytest
import respx

from tests.test_bridge import GRAPHQL, SPOOLMAN, UUID, mock_fields
from tests.test_web import configured, graphql, signed_in


def dashboard(respx_mock, tmp_path, remaining, dry_run="false", track_usage="true"):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    mock_fields(respx_mock)
    spool = {"id": 52, "location": "Core One", "extra": {"opt_instance_uuid": json.dumps(UUID), "opt_consumed_weight": "0"}}
    if remaining is not None:
        spool["remaining_weight"] = remaining
    respx_mock.get(url__startswith=f"{SPOOLMAN}/api/v1/spool").respond(json=[spool])
    service = configured(tmp_path, LOCATION_NAME="Core One", DRY_RUN=dry_run, TRACK_USAGE=track_usage)
    assert service.poll_once() is True
    return signed_in(service).get("/").text


@respx.mock(assert_all_called=False)
def test_a_linked_spool_shows_what_spoolman_has_left(respx_mock, tmp_path):
    # Connect reports 959 g left for the default test spool.
    page = dashboard(respx_mock, tmp_path, 955)
    assert "Spoolman: <strong>955 g</strong> left" in page
    assert "than Connect" not in page


@pytest.mark.parametrize("remaining,text", [(853, "106 g less than Connect"), (1000, "41 g more than Connect"), (969, "10 g more than Connect"), (949, "10 g less than Connect")])
@respx.mock(assert_all_called=False)
def test_a_difference_is_pointed_out(respx_mock, tmp_path, remaining, text):
    page = dashboard(respx_mock, tmp_path, remaining)
    assert text in page
    assert "Spoolman and Connect disagree" in page


@respx.mock(assert_all_called=False)
def test_nothing_is_shown_without_a_spoolman_weight(respx_mock, tmp_path):
    page = dashboard(respx_mock, tmp_path, None)
    assert "Spoolman:" not in page


@pytest.mark.parametrize("remaining", [968, 950])
@respx.mock(assert_all_called=False)
def test_less_than_10_g_apart_is_not_pointed_out(respx_mock, tmp_path, remaining):
    assert "than Connect" not in dashboard(respx_mock, tmp_path, remaining)


@respx.mock(assert_all_called=False)
def test_a_dry_run_says_the_difference_is_not_deducted_yet(respx_mock, tmp_path):
    page = dashboard(respx_mock, tmp_path, 1000, dry_run="true")
    assert "41 g not deducted yet" in page
    assert "deducted when you go live" in page
    assert "than Connect" not in page and "Spoolman and Connect disagree" not in page


@respx.mock(assert_all_called=False)
def test_a_dry_run_still_warns_when_spoolman_has_less(respx_mock, tmp_path):
    page = dashboard(respx_mock, tmp_path, 853, dry_run="true")
    assert "106 g less than Connect" in page
    assert "consumption before the spool was linked" in page


@respx.mock(assert_all_called=False)
def test_a_dry_run_with_tracking_off_never_promises_a_deduction(respx_mock, tmp_path):
    page = dashboard(respx_mock, tmp_path, 1000, dry_run="true", track_usage="false")
    assert "not deducted yet" not in page and "deducted when you go live" not in page
    assert "41 g more than Connect" in page


@respx.mock(assert_all_called=False)
def test_tracking_off_says_spoolman_weight_comes_from_elsewhere(respx_mock, tmp_path):
    page = dashboard(respx_mock, tmp_path, 853, track_usage="false")
    assert "106 g less than Connect" in page
    assert "Deducting is off" in page
    assert "only deducts new consumption" not in page
