import json

import respx

from bridge.config import read_config
from tests.test_bridge import GRAPHQL, SPOOLMAN, UUID, mock_fields, printer
from tests.test_web import configured, graphql, signed_in, spoolman_writes


def consuming_graphql(consumed):
    def handler(request):
        if "BridgePrinter(" not in json.loads(request.content)["query"]:
            return graphql(request)
        return respx.MockResponse(json={"data": {"printer": {"printer": printer(consumed=consumed)}}})
    return handler


def dry_run_with_linked_spool(respx_mock, tmp_path, consumed, marker):
    respx_mock.post(GRAPHQL).mock(side_effect=consuming_graphql(consumed))
    mock_fields(respx_mock)
    extra = {"opt_instance_uuid": json.dumps(UUID)}
    if marker is not None:
        extra["opt_consumed_weight"] = json.dumps(marker)
    spool = {"id": 52, "location": "Core One", "used_weight": 100, "extra": extra}
    respx_mock.get(url__startswith=f"{SPOOLMAN}/api/v1/spool?").respond(json=[spool])
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/52").respond(json=spool)
    service = configured(tmp_path, LOCATION_NAME="Core One")
    assert service.settings.dry_run is True
    assert service.poll_once() is True
    return service


@respx.mock(assert_all_called=False)
def test_the_dry_run_band_links_to_going_live(respx_mock, tmp_path):
    service = dry_run_with_linked_spool(respx_mock, tmp_path, consumed=106, marker=100)
    page = signed_in(service).get("/").text
    assert 'href="/go-live"' in page


@respx.mock(assert_all_called=False)
def test_go_live_shows_what_would_be_deducted_and_writes_nothing(respx_mock, tmp_path):
    service = dry_run_with_linked_spool(respx_mock, tmp_path, consumed=106, marker=100)
    page = signed_in(service).get("/go-live").text
    assert "Turn dry run off?" in page
    assert "#52 Prusament PC Blend Jet Black" in page
    assert "100 g" in page and "106 g" in page
    assert "<strong>6 g</strong>" in page
    assert spoolman_writes(respx_mock) == []
    assert service.settings.dry_run is True


@respx.mock(assert_all_called=False)
def test_go_live_says_a_new_spool_starts_counting(respx_mock, tmp_path):
    service = dry_run_with_linked_spool(respx_mock, tmp_path, consumed=40, marker=None)
    # The dry run stored its first sighting as the baseline.
    page = signed_in(service).get("/go-live").text
    assert "<strong>0 g</strong>" in page
    service.status.clear()
    preview = service.go_live_preview()
    assert preview["rows"] == [] and preview["total"] == 0


@respx.mock(assert_all_called=False)
def test_going_live_turns_dry_run_off(respx_mock, tmp_path):
    service = dry_run_with_linked_spool(respx_mock, tmp_path, consumed=106, marker=100)
    client = signed_in(service)
    response = client.post("/go-live")
    assert response.headers["Location"].endswith("/")
    assert read_config(tmp_path)["dry_run"] is False
    assert service.settings.dry_run is False
    assert client.get("/go-live").headers["Location"].endswith("/")
    assert "Go live" not in client.get("/").text


@respx.mock(assert_all_called=False)
def test_go_live_finds_a_spool_linked_after_the_last_poll(respx_mock, tmp_path):
    service = dry_run_with_linked_spool(respx_mock, tmp_path, consumed=106, marker=100)
    # The link moved to spool 60 in Spoolman after the poll.
    moved = {"id": 60, "location": "Core One", "extra": {"opt_instance_uuid": json.dumps(UUID), "opt_consumed_weight": json.dumps(103)}}
    respx_mock.get(url__startswith=f"{SPOOLMAN}/api/v1/spool?").respond(json=[moved])
    preview = service.go_live_preview()
    assert [(row["spool_id"], row["deduct"]) for row in preview["rows"]] == [(60, 3.0)]
    assert preview["unlinked"] == []


def test_go_live_needs_a_finished_setup(tmp_path):
    from tests.test_web import make_service
    service = make_service(tmp_path)
    client = signed_in(service)
    assert client.post("/go-live").headers["Location"].endswith("/setup/spoolman")
    assert "dry_run" not in read_config(tmp_path)
