import base64
import copy
import json
import os
import stat
import time

import httpx
import pytest
import respx

from bridge.config import ConfigError, PrinterConfig, Settings, load_settings, update_config
from bridge.connect import ConnectAuthError, ConnectClient, ConnectError, TokenStore, Tokens, jwt_expiry, parse_login_json
from bridge.spoolman import SpoolmanClient, SpoolmanError, extra_value, tls_verify
from bridge.sync import (
    Bridge,
    State,
    color_hex,
    consumption_step,
    fill_from_warehouse,
    parse_warehouse_items,
    index_spools,
    parse_loaded_spools,
    build_link_changes,
    can_replace_link,
    rank_spools_for_link,
    score_spool,
)

SPOOLMAN = "https://spoolman.test"
GRAPHQL = "https://connect.test/graphql"
ACCOUNT = "https://account.test"
UUID = "2ed54568-7913-5aa0-9965-77da40c0af29"
MATERIAL_UUID = "517f24cc-16a4-5091-8325-5f6873e9dfcb"


def make_jwt(exp: float) -> str:
    def encode(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{encode({'alg': 'none'})}.{encode({'exp': exp})}.sig"


def settings(tmp_path, dry_run=False) -> Settings:
    return Settings(
        spoolman_url=SPOOLMAN,
        dry_run=dry_run,
        data_dir=tmp_path,
        account_url=ACCOUNT,
        account_client_id="client-1",
        graphql_url=GRAPHQL,
    )


PRINTER = PrinterConfig(uuid="printer-1", organization_id="org-1", name="Core", location="Core One")


def printer(consumed=0, uuid=UUID, from_nfc=True):
    return {
        "__typename": "Printer",
        "id": "printer-1",
        "name": "Core",
        "state": "IDLE",
        "fff": {
            "tools": [
                {
                    "id": "0",
                    "materialPackageInstance": {
                        "__typename": "FFFToolMaterialPackageInstance",
                        "uuid": uuid,
                        "actualNettoFullWeight": 959,
                        "isFromNfc": from_nfc,
                        "dynamicData": {"consumedWeight": consumed},
                        "package": {
                            "nominalNettoFullWeight": 1000,
                            "filamentDiameter": 1.75,
                            "container": {"emptyWeight": 277},
                            "material": {
                                "uuid": MATERIAL_UUID,
                                "name": "PC Blend Jet Black",
                                "abbreviation": "PC",
                                "brand": {"name": "Prusament"},
                                "primaryColor": {"colorRgba": "#212929"},
                                "type": {"abbreviation": "PC"},
                                "properties": {"density": 1.22},
                            },
                        },
                    },
                }
            ]
        },
    }


class FakeConnect:
    def __init__(self, data, warehouse=()):
        self.data = data
        self.warehouse = warehouse

    def fetch_printer(self, organization_id, printer_uuid):
        return self.data

    def warehouse_items(self, organization_id):
        if isinstance(self.warehouse, Exception):
            raise self.warehouse
        return self.warehouse


def linked_extra(consumed=None, **more):
    extra = {"opt_instance_uuid": json.dumps(UUID), **more}
    if consumed is not None:
        extra["opt_consumed_weight"] = json.dumps(consumed)
    return extra


def existing_spool(location="Core One", extra=None, used=100.0):
    """A linked spool whose marker says it accounts for 0 g, which is what printer() reports by default."""
    return {
        "id": 42,
        "location": location,
        "archived": False,
        "used_weight": used,
        "first_used": "2026-10-01T10:00:00Z",
        "extra": extra if extra is not None else linked_extra(0),
    }


def mock_fields(mock, present=True):
    fields = [{"key": "opt_instance_uuid"}, {"key": "opt_consumed_weight"}] if present else []
    filament_fields = [{"key": "opt_material_uuid"}] if present else []
    mock.get(f"{SPOOLMAN}/api/v1/field/spool").respond(json=fields)
    mock.get(f"{SPOOLMAN}/api/v1/field/filament").respond(json=filament_fields)


class Poller:
    def __init__(self, bridge):
        self.bridge = bridge

    def poll(self):
        return self.bridge.poll(PRINTER)


def make_bridge(tmp_path, data, dry_run=False, auto_create=True):
    s = settings(tmp_path, dry_run)
    return Poller(Bridge(FakeConnect(data), SpoolmanClient(s, httpx.Client()), State(s.state_path, dry_run), auto_create))


def writes(mock):
    return [c.request for c in mock.calls if c.request.method != "GET"]


# Consumption


@pytest.mark.parametrize(
    "previous,current,expected",
    [
        (None, 0, ("baseline", 0.0)),
        (None, 50, ("baseline", 0.0)),
        (10, 10, ("none", 0.0)),
        (10, 12.5, ("use", 2.5)),
        (10, 4, ("rebase", 0.0)),
    ],
)
def test_consumption_step(previous, current, expected):
    assert consumption_step(previous, current) == expected


@respx.mock(assert_all_called=False)
def test_first_sighting_of_existing_spool_only_sets_baseline(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool(extra=linked_extra())])
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/42").respond(json={})
    make_bridge(tmp_path, printer(consumed=100)).poll()
    assert [json.loads(c.request.content) for c in patch.calls] == [{"extra": linked_extra(100.0)}]
    assert len(writes(respx_mock)) == 1
    assert json.loads((tmp_path / "state.json").read_text())["consumed"][UUID] == 100


def mock_deduction(mock, spool):
    mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[spool])
    mock.get(f"{SPOOLMAN}/api/v1/spool/42").respond(json=spool)
    return mock.patch(f"{SPOOLMAN}/api/v1/spool/42").respond(json={})


@respx.mock(assert_all_called=False)
def test_increase_deducts_the_delta_and_moves_the_marker_in_one_write(respx_mock, tmp_path):
    mock_fields(respx_mock)
    patch = mock_deduction(respx_mock, existing_spool(extra=linked_extra(10, card_uids=json.dumps("ABC"))))
    make_bridge(tmp_path, printer(consumed=12.5)).poll()
    assert patch.call_count == 1
    body = json.loads(patch.calls[0].request.content)
    assert body["used_weight"] == 102.5
    assert body["extra"] == linked_extra(12.5, card_uids=json.dumps("ABC"))
    assert "last_used" in body and "first_used" not in body
    assert respx_mock.calls.last.request.method == "PATCH"
    assert json.loads((tmp_path / "state.json").read_text())["consumed"][UUID] == 12.5


@respx.mock(assert_all_called=False)
def test_a_spool_linked_before_the_marker_uses_the_local_state(respx_mock, tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"consumed": {UUID: 10}}))
    mock_fields(respx_mock)
    patch = mock_deduction(respx_mock, existing_spool(extra=linked_extra()))
    make_bridge(tmp_path, printer(consumed=12.5)).poll()
    body = json.loads(patch.calls[0].request.content)
    assert body["used_weight"] == 102.5
    assert body["extra"]["opt_consumed_weight"] == "12.5"


@respx.mock(assert_all_called=False)
def test_the_marker_in_spoolman_wins_over_the_local_state(respx_mock, tmp_path):
    """Another bridge, or this one before a reinstall, already deducted up to 12 g."""
    (tmp_path / "state.json").write_text(json.dumps({"consumed": {UUID: 6}}))
    mock_fields(respx_mock)
    patch = mock_deduction(respx_mock, existing_spool(extra=linked_extra(12)))
    make_bridge(tmp_path, printer(consumed=12)).poll()
    assert patch.call_count == 0
    assert writes(respx_mock) == []


@respx.mock(assert_all_called=False)
def test_two_bridges_deduct_each_gram_only_once(respx_mock, tmp_path):
    spool = existing_spool(extra=linked_extra(0))
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[spool])
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/42").mock(side_effect=lambda request: httpx.Response(200, json=spool))

    def apply(request):
        body = json.loads(request.content)
        spool.update(used_weight=body.get("used_weight", spool["used_weight"]), extra=body["extra"])
        return httpx.Response(200, json=spool)

    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/42").mock(side_effect=apply)
    # Both read the spool list before either has written.
    first = make_bridge(tmp_path / "a", printer(consumed=6))
    second = make_bridge(tmp_path / "b", printer(consumed=6))
    first.poll()
    second.poll()
    assert spool["used_weight"] == 106
    assert patch.call_count == 1


@respx.mock(assert_all_called=False)
def test_a_deduction_that_another_bridge_partly_made_only_deducts_the_rest(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool(extra=linked_extra(0))])
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/42").respond(json=existing_spool(extra=linked_extra(4), used=104))
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/42").respond(json={})
    make_bridge(tmp_path, printer(consumed=6)).poll()
    assert json.loads(patch.calls[0].request.content)["used_weight"] == 106


@respx.mock(assert_all_called=False)
def test_first_use_sets_first_used(respx_mock, tmp_path):
    mock_fields(respx_mock)
    spool = existing_spool(extra=linked_extra(0))
    spool["first_used"] = None
    patch = mock_deduction(respx_mock, spool)
    make_bridge(tmp_path, printer(consumed=3)).poll()
    body = json.loads(patch.calls[0].request.content)
    assert body["first_used"] == body["last_used"]


@respx.mock(assert_all_called=False)
def test_state_is_unchanged_when_the_deduction_fails(respx_mock, tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"consumed": {UUID: 10}}))
    mock_fields(respx_mock)
    spool = existing_spool(extra=linked_extra(10))
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[spool])
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/42").respond(json=spool)
    respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/42").respond(500, text="boom")
    with pytest.raises(Exception):
        make_bridge(tmp_path, printer(consumed=12.5)).poll()
    assert json.loads((tmp_path / "state.json").read_text())["consumed"][UUID] == 10


@respx.mock(assert_all_called=False)
def test_decrease_rebases_and_only_moves_the_marker(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool(extra=linked_extra(10))])
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/42").respond(json={})
    make_bridge(tmp_path, printer(consumed=4)).poll()
    assert [json.loads(c.request.content) for c in patch.calls] == [{"extra": linked_extra(4.0)}]
    assert json.loads((tmp_path / "state.json").read_text())["consumed"][UUID] == 4


@respx.mock(assert_all_called=False)
def test_more_than_a_whole_spool_in_one_step_is_not_deducted(respx_mock, tmp_path, caplog):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool(extra=linked_extra(10))])
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/42").respond(json={})
    make_bridge(tmp_path, printer(consumed=1500)).poll()
    assert [json.loads(c.request.content) for c in patch.calls] == [{"extra": linked_extra(1500.0)}]
    assert "more than a whole spool" in caplog.text


# Mapping and parsing


def test_extra_values_are_json_decoded():
    assert extra_value({"extra": {"opt_instance_uuid": json.dumps(UUID)}}, "opt_instance_uuid") == UUID
    assert extra_value({"extra": {}}, "opt_instance_uuid") is None


def test_index_uses_only_the_instance_uuid_and_ignores_card_uids():
    spools = [
        {"id": 1, "extra": {"card_uids": json.dumps(UUID)}},
        {"id": 2, "extra": {"opt_instance_uuid": json.dumps(UUID)}},
        {"id": 3, "extra": {}},
    ]
    assert index_spools(spools)[UUID]["id"] == 2
    assert len(index_spools(spools)) == 1


@pytest.mark.parametrize(
    "rgba,expected",
    [("#212929", "212929"), ("#212929ff", "212929"), ("bad", None), (None, None)],
)
def test_color_hex(rgba, expected):
    assert color_hex(rgba) == expected


def test_empty_tool_gives_no_spool():
    data = {"fff": {"tools": [{"id": "0", "materialPackageInstance": {"__typename": "ToolMaterialPackageInstanceEmpty", "reason": "EMPTY"}}]}}
    assert parse_loaded_spools(data) == []


def test_parse_loaded_spool():
    spool = parse_loaded_spools(printer())[0]
    assert (spool.uuid, spool.brand, spool.material_name) == (UUID, "Prusament", "PC Blend Jet Black")
    assert (spool.actual_weight, spool.empty_weight, spool.nominal_weight) == (959, 277, 1000)
    assert (spool.color_hex, spool.density, spool.filament_diameter) == ("212929", 1.22, 1.75)


# Creating and updating in Spoolman


@respx.mock(assert_all_called=False)
def test_unknown_instance_creates_vendor_filament_and_spool(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[])
    respx_mock.get(f"{SPOOLMAN}/api/v1/vendor").respond(json=[])
    respx_mock.get(f"{SPOOLMAN}/api/v1/filament").respond(json=[])
    vendor = respx_mock.post(f"{SPOOLMAN}/api/v1/vendor").respond(json={"id": 7})
    filament = respx_mock.post(f"{SPOOLMAN}/api/v1/filament").respond(json={"id": 8})
    spool = respx_mock.post(f"{SPOOLMAN}/api/v1/spool").respond(json={"id": 9})
    make_bridge(tmp_path, printer(consumed=30)).poll()

    assert json.loads(vendor.calls[0].request.content) == {"name": "Prusament"}
    filament_body = json.loads(filament.calls[0].request.content)
    assert filament_body["vendor_id"] == 7
    assert filament_body["color_hex"] == "212929"
    assert filament_body["weight"] == 1000
    assert filament_body["spool_weight"] == 277
    assert filament_body["extra"] == {"opt_material_uuid": json.dumps(MATERIAL_UUID)}
    spool_body = json.loads(spool.calls[0].request.content)
    assert spool_body["filament_id"] == 8
    assert spool_body["initial_weight"] == 959
    assert spool_body["spool_weight"] == 277
    assert spool_body["used_weight"] == 30
    assert spool_body["location"] == "Core One"
    assert spool_body["extra"] == linked_extra(30.0)
    assert "card_uids" not in spool_body["extra"]
    assert json.loads((tmp_path / "state.json").read_text())["consumed"][UUID] == 30


@respx.mock(assert_all_called=False)
def test_existing_vendor_and_filament_are_reused(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[])
    respx_mock.get(f"{SPOOLMAN}/api/v1/vendor").respond(json=[{"id": 7, "name": "prusament"}])
    respx_mock.get(f"{SPOOLMAN}/api/v1/filament").respond(
        json=[{"id": 8, "name": "PC Blend Jet Black", "vendor": {"id": 7}}]
    )
    spool = respx_mock.post(f"{SPOOLMAN}/api/v1/spool").respond(json={"id": 9})
    make_bridge(tmp_path, printer()).poll()
    assert len(writes(respx_mock)) == 1
    assert json.loads(spool.calls[0].request.content)["filament_id"] == 8


@respx.mock(assert_all_called=False)
def test_location_update_does_not_send_extra_fields(respx_mock, tmp_path):
    mock_fields(respx_mock)
    spool = existing_spool(location="Kjeller", extra={"opt_instance_uuid": json.dumps(UUID), "card_uids": json.dumps("ABC")})
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[spool])
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/42").respond(json={})
    make_bridge(tmp_path, printer()).poll()
    assert json.loads(patch.calls[0].request.content) == {"location": "Core One"}


@respx.mock(assert_all_called=False)
def test_location_is_not_written_when_it_already_matches(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool()])
    poller = make_bridge(tmp_path, printer())
    poller.poll()
    poller.poll()
    assert writes(respx_mock) == []


@respx.mock(assert_all_called=False)
def test_location_is_set_once_when_something_else_keeps_changing_it(respx_mock, tmp_path, caplog):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool(location="Another bridge")])
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/42").respond(json={})
    poller = make_bridge(tmp_path, printer())
    for _ in range(3):
        poller.poll()
    assert patch.call_count == 1
    assert sum("only one bridge" in r.message for r in caplog.records) == 1


@respx.mock(assert_all_called=False)
def test_location_is_set_again_when_the_spool_is_loaded_again(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool(location="Shelf")])
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/42").respond(json={})
    poller = make_bridge(tmp_path, printer())
    poller.poll()
    poller.bridge.connect.data = {**printer(), "fff": {"tools": []}}
    poller.poll()
    poller.bridge.connect.data = printer()
    poller.poll()
    assert patch.call_count == 2


@respx.mock(assert_all_called=False)
def test_a_failed_location_write_is_tried_again(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool(location="Shelf")])
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/42")
    patch.side_effect = [httpx.Response(500), httpx.Response(200, json={})]
    poller = make_bridge(tmp_path, printer())
    with pytest.raises(SpoolmanError):
        poller.poll()
    poller.poll()
    assert patch.call_count == 2


@respx.mock(assert_all_called=False)
def test_missing_extra_fields_are_created(respx_mock, tmp_path):
    mock_fields(respx_mock, present=False)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool()])
    spool_field = respx_mock.post(f"{SPOOLMAN}/api/v1/field/spool/opt_instance_uuid").respond(json=[])
    consumed_field = respx_mock.post(f"{SPOOLMAN}/api/v1/field/spool/opt_consumed_weight").respond(json=[])
    filament_field = respx_mock.post(f"{SPOOLMAN}/api/v1/field/filament/opt_material_uuid").respond(json=[])
    make_bridge(tmp_path, printer()).poll()
    assert json.loads(spool_field.calls[0].request.content)["field_type"] == "text"
    assert json.loads(consumed_field.calls[0].request.content)["field_type"] == "float"
    assert filament_field.called


@respx.mock(assert_all_called=False)
def test_auto_create_skips_a_tag_without_density_and_keeps_polling(respx_mock, tmp_path, caplog):
    """A tag written without density cannot become a filament; that must not fail the whole poll."""
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[])
    data = printer(consumed=30)
    data["fff"]["tools"][0]["materialPackageInstance"]["package"]["material"]["properties"] = {"density": None}
    result = make_bridge(tmp_path, data, auto_create=True).poll()
    assert result["tools"][0]["spool_id"] is None
    assert writes(respx_mock) == []
    assert any("density" in w for w in result["warnings"])


@respx.mock(assert_all_called=False)
def test_auto_create_skip_leaves_the_other_tools_synced(respx_mock, tmp_path):
    """The tag without density only skips its own tool; a linked spool in the next tool is still synced."""
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool(extra=linked_extra(30))])
    data = printer(consumed=30)
    linked_tool = copy.deepcopy(data["fff"]["tools"][0])
    linked_tool["id"] = "1"
    bare = data["fff"]["tools"][0]["materialPackageInstance"]
    bare["uuid"] = "no-density"
    bare["package"]["material"]["properties"] = {"density": None}
    data["fff"]["tools"].append(linked_tool)
    result = make_bridge(tmp_path, data, auto_create=True).poll()
    assert [tool["spool_id"] for tool in result["tools"]] == [None, 42]
    assert writes(respx_mock) == []


@respx.mock(assert_all_called=False)
def test_auto_create_uses_density_from_the_warehouse(respx_mock, tmp_path):
    """Adding the density in Connect is enough: the warehouse fills it in and the spool is created."""
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[])
    respx_mock.get(f"{SPOOLMAN}/api/v1/vendor").respond(json=[])
    respx_mock.get(f"{SPOOLMAN}/api/v1/filament").respond(json=[])
    respx_mock.post(f"{SPOOLMAN}/api/v1/vendor").respond(json={"id": 7})
    filament = respx_mock.post(f"{SPOOLMAN}/api/v1/filament").respond(json={"id": 8})
    spool = respx_mock.post(f"{SPOOLMAN}/api/v1/spool").respond(json={"id": 9})
    data = printer(consumed=30)
    data["fff"]["tools"][0]["materialPackageInstance"]["package"]["material"]["properties"] = {"density": None}
    item = {"materialPackageInstance": {"uuid": UUID, "package": {"material": {"properties": {"density": 1.2}}}}}
    poller = make_bridge(tmp_path, data, auto_create=True)
    poller.bridge.connect.warehouse = [item]
    result = poller.poll()
    assert not any("density" in w for w in result["warnings"])
    assert json.loads(filament.calls[0].request.content)["density"] == 1.2
    assert spool.called
    assert result["tools"][0]["spool_id"] == 9


def custom_tag_printer(consumed=0):
    data = printer(consumed=consumed, uuid="custom", from_nfc=False)
    instance = data["fff"]["tools"][0]["materialPackageInstance"]
    instance["actualNettoFullWeight"] = None
    instance["package"]["container"] = None
    material = instance["package"]["material"]
    material.update(name=None, brand=None, properties={"density": None}, primaryColor={"colorRgba": "#101010"})
    material["type"] = {"abbreviation": "PETG"}
    return data


@respx.mock(assert_all_called=False)
def test_a_linked_spool_without_nfc_reading_is_still_tracked(respx_mock, tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"consumed": {"custom": 3}}))
    mock_fields(respx_mock)
    spool = {"id": 55, "location": "Core One", "used_weight": 0, "extra": {"opt_instance_uuid": json.dumps("custom")}}
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[spool])
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/55").respond(json=spool)
    use = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/55").respond(json={})
    result = make_bridge(tmp_path, custom_tag_printer(consumed=8), auto_create=False).poll()
    assert json.loads(use.calls[0].request.content)["used_weight"] == 5
    assert result["tools"][0]["spool_id"] == 55


def test_a_custom_tag_spool_keeps_spoolmans_filament_by_default():
    loaded = parse_loaded_spools(custom_tag_printer())[0]
    assert loaded.can_create is False
    assert loaded.display_name == "Unknown brand PETG"
    spool = {"id": 55, "initial_weight": 1000, "spool_weight": None, "used_weight": 0, "filament": {"id": 5, "name": "petg-gf", "material": "PETG", "color_hex": "000000", "weight": 1000, "extra": {}}, "extra": {}}
    changes = {c["key"]: c for c in build_link_changes(loaded, spool, 0)}
    assert set(changes) == {"spool_instance", "filament_color_hex", "filament_material"}
    assert changes["filament_color_hex"]["checked"] is False
    assert changes["filament_material"]["checked"] is False


@respx.mock(assert_all_called=False)
def test_non_nfc_instance_is_never_created_automatically(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[])
    make_bridge(tmp_path, printer(from_nfc=False)).poll()
    assert writes(respx_mock) == []


# Dry run


@respx.mock(assert_all_called=False)
def test_dry_run_sends_no_writes_and_leaves_no_state(respx_mock, tmp_path, caplog):
    caplog.set_level("INFO")
    mock_fields(respx_mock, present=False)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[])
    respx_mock.get(f"{SPOOLMAN}/api/v1/vendor").respond(json=[])
    respx_mock.get(f"{SPOOLMAN}/api/v1/filament").respond(json=[])
    make_bridge(tmp_path, printer(), dry_run=True).poll()
    assert writes(respx_mock) == []
    for expected in (
        "POST /field/spool/opt_instance_uuid",
        "POST /field/filament/opt_material_uuid",
        "POST /vendor",
        "POST /filament",
        "POST /spool",
    ):
        assert f"DRY-RUN would call {expected} " in caplog.text


@respx.mock(assert_all_called=False)
def test_dry_run_logs_a_deduction_only_once(respx_mock, tmp_path, caplog):
    caplog.set_level("INFO")
    (tmp_path / "state.json").write_text(json.dumps({"consumed": {UUID: 10}}))
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool(extra=linked_extra())])
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/42").respond(json=existing_spool(extra=linked_extra()))
    bridge = make_bridge(tmp_path, printer(consumed=15), dry_run=True)
    bridge.poll()
    bridge.poll()
    assert writes(respx_mock) == []
    assert caplog.text.count('DRY-RUN would call PATCH /spool/42 {"used_weight": 105.0') == 1
    assert json.loads((tmp_path / "state.json").read_text())["consumed"][UUID] == 10


# Connect tokens


def connect_client(tmp_path, access, refresh="refresh-1"):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(Tokens(access, refresh))
    return ConnectClient(settings(tmp_path), store, httpx.Client()), store


def graphql_ok(data=None):
    return {"json": {"data": {"printer": {"printer": data or printer()}}}}


def test_jwt_expiry():
    assert jwt_expiry(make_jwt(123.5)) == 123.5
    assert jwt_expiry("not-a-jwt") is None


def test_token_file_is_private(tmp_path):
    store = TokenStore(tmp_path / "tokens.json")
    store.save(Tokens("a", "b"))
    assert stat.S_IMODE(os.stat(tmp_path / "tokens.json").st_mode) == 0o600


@respx.mock(assert_all_called=False)
def test_valid_token_is_used_without_refreshing(respx_mock, tmp_path):
    access = make_jwt(time.time() + 3600)
    client, _ = connect_client(tmp_path, access)
    query = respx_mock.post(GRAPHQL).respond(**graphql_ok())
    refresh = respx_mock.post(f"{ACCOUNT}/o/token/").respond(500)
    assert client.fetch_printer("org-1", "printer-1")["name"] == "Core"
    assert query.calls[0].request.headers["authorization"] == f"Bearer {access}"
    assert not refresh.called


@respx.mock(assert_all_called=False)
def test_expiring_token_is_refreshed_and_rotation_is_saved(respx_mock, tmp_path):
    new_access = make_jwt(time.time() + 3600)
    client, store = connect_client(tmp_path, make_jwt(time.time() + 10))
    refresh = respx_mock.post(f"{ACCOUNT}/o/token/").respond(
        json={"access_token": new_access, "refresh_token": "refresh-2"}
    )
    query = respx_mock.post(GRAPHQL).respond(**graphql_ok())
    client.fetch_printer("org-1", "printer-1")
    form = refresh.calls[0].request.content.decode()
    assert "grant_type=refresh_token" in form and "refresh_token=refresh-1" in form and "client_id=client-1" in form
    assert query.calls[0].request.headers["authorization"] == f"Bearer {new_access}"
    assert store.load() == Tokens(new_access, "refresh-2")


@respx.mock(assert_all_called=False)
def test_rejected_refresh_keeps_the_stored_tokens(respx_mock, tmp_path):
    old_access = make_jwt(time.time() + 10)
    client, store = connect_client(tmp_path, old_access)
    respx_mock.post(f"{ACCOUNT}/o/token/").respond(400, json={"error": "invalid_grant"})
    with pytest.raises(ConnectAuthError, match="invalid_grant"):
        client.fetch_printer("org-1", "printer-1")
    assert store.load() == Tokens(old_access, "refresh-1")


@respx.mock(assert_all_called=False)
def test_401_triggers_one_refresh_and_retry(respx_mock, tmp_path):
    new_access = make_jwt(time.time() + 3600)
    client, _ = connect_client(tmp_path, make_jwt(time.time() + 3600))
    respx_mock.post(f"{ACCOUNT}/o/token/").respond(json={"access_token": new_access})
    query = respx_mock.post(GRAPHQL).mock(
        side_effect=[httpx.Response(401, json={"errors": []}), httpx.Response(200, **graphql_ok())]
    )
    assert client.fetch_printer("org-1", "printer-1")["name"] == "Core"
    assert query.call_count == 2


def test_expired_token_without_refresh_token_asks_for_a_new_login(tmp_path):
    client, _ = connect_client(tmp_path, make_jwt(time.time() - 10), refresh="")
    with pytest.raises(ConnectAuthError, match="log in to Connect again"):
        client.fetch_printer("org-1", "printer-1")


# Settings


def test_dry_run_and_no_auto_create_are_the_defaults(tmp_path):
    s = load_settings({"DATA_DIR": str(tmp_path), "SPOOLMAN_URL": "https://s/"})
    assert s.dry_run is True
    assert s.auto_create is False
    assert s.spoolman_verify_tls is True
    assert s.spoolman_url == "https://s"
    assert s.printers == ()


def test_old_env_file_still_describes_one_printer(tmp_path):
    s = load_settings({"DATA_DIR": str(tmp_path), "CONNECT_PRINTER_UUID": "p", "CONNECT_ORGANIZATION_ID": "o", "LOCATION_NAME": "Shelf", "DRY_RUN": "false"})
    assert s.printers == (PrinterConfig(uuid="p", organization_id="o", location="Shelf"),)
    assert s.dry_run is False


def test_config_file_wins_over_env(tmp_path):
    update_config(tmp_path, {"dry_run": False, "spoolman_url": "http://saved", "printers": [{"uuid": "a", "organization_id": "b", "name": "Core"}]})
    s = load_settings({"DATA_DIR": str(tmp_path), "DRY_RUN": "true", "SPOOLMAN_URL": "http://env", "POLL_INTERVAL": "30", "CONNECT_PRINTER_UUID": "p", "CONNECT_ORGANIZATION_ID": "o"})
    assert s.dry_run is False
    assert s.spoolman_url == "http://saved"
    assert s.poll_interval == 30
    assert s.printers == (PrinterConfig(uuid="a", organization_id="b", name="Core", location="Core"),)


def test_broken_config_file_is_a_config_error(tmp_path):
    (tmp_path / "config.json").write_text("{nope")
    with pytest.raises(ConfigError, match="config.json"):
        load_settings({"DATA_DIR": str(tmp_path)})


def test_missing_ca_bundle_is_a_config_error(tmp_path):
    s = Settings(spoolman_url=SPOOLMAN, spoolman_ca_bundle=str(tmp_path / "missing.crt"))
    with pytest.raises(ConfigError, match="SPOOLMAN_CA_BUNDLE"):
        tls_verify(s)


def test_plain_http_does_not_need_the_ca_bundle(tmp_path):
    s = Settings(spoolman_url="http://spoolman.test:7912", spoolman_ca_bundle=str(tmp_path / "missing.crt"))
    assert tls_verify(s) is True


def test_login_json_is_parsed():
    assert parse_login_json('{"access": "a", "refresh": "r"}') == ("a", "r")
    assert parse_login_json('{"access": "a", "refresh": null}') == ("a", "")
    assert parse_login_json('"just-a-token"') is None
    assert parse_login_json("just-a-token") is None
    assert parse_login_json('{"access": null, "refresh": "r"}') is None


# Dry run to live


@respx.mock(assert_all_called=False)
def test_going_live_deducts_what_was_used_during_the_dry_run(respx_mock, tmp_path):
    mock_fields(respx_mock)
    spool = existing_spool(extra=linked_extra())
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").mock(side_effect=lambda request: httpx.Response(200, json=[spool]))
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/42").mock(side_effect=lambda request: httpx.Response(200, json=spool))
    make_bridge(tmp_path, printer(consumed=5), dry_run=True).poll()
    assert writes(respx_mock) == []

    def apply(request):
        body = json.loads(request.content)
        spool.update(used_weight=body.get("used_weight", spool["used_weight"]), extra=body["extra"])
        return httpx.Response(200, json=spool)

    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/42").mock(side_effect=apply)
    make_bridge(tmp_path, printer(consumed=20)).poll()
    assert json.loads(patch.calls[0].request.content)["used_weight"] == 115
    make_bridge(tmp_path, printer(consumed=20)).poll()
    assert patch.call_count == 1


# Linking


@respx.mock(assert_all_called=False)
def test_unknown_spool_waits_for_a_choice_without_auto_create(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[])
    result = make_bridge(tmp_path, printer(), auto_create=False).poll()
    assert writes(respx_mock) == []
    assert result["tools"][0]["spool_id"] is None


def linker(tmp_path):
    return make_bridge(tmp_path, printer(consumed=30), auto_create=False).bridge


SPOOL_52 = {
    "id": 52,
    "initial_weight": 1000.0,
    "spool_weight": 186.0,
    "used_weight": 0.0,
    "remaining_weight": 1000.0,
    "extra": {"card_uids": json.dumps("ABC")},
    "filament": {"id": 49, "name": "Blend Jet Black", "material": "PC", "color_hex": "24292A", "weight": 1000.0, "spool_weight": 186.0, "extra": {"variant": json.dumps("x")}, "vendor": {"name": "Prusament"}},
}
ALL_KEYS = {"spool_initial_weight", "spool_spool_weight", "spool_used_weight", "filament_name", "filament_color_hex", "filament_spool_weight", "filament_material"}


def loaded_spool(consumed=30):
    return parse_loaded_spools(printer(consumed=consumed))[0]


def test_the_real_case_is_a_high_confidence_match():
    other = {**SPOOL_52, "id": 53, "remaining_weight": 800, "filament": {**SPOOL_52["filament"], "name": "Blend Natural", "color_hex": "EEEEEE"}}
    ranked = rank_spools_for_link(loaded_spool(0), [other, SPOOL_52])
    assert ranked[0]["id"] == 52
    assert ranked[0]["best"] is True
    assert ranked[0]["confidence"] == "High"
    assert "Same brand" in ranked[0]["reasons"]
    assert any(r.startswith("Name 9") for r in ranked[0]["reasons"])
    assert any(r.startswith("Color very close") for r in ranked[0]["reasons"])
    assert ranked[1]["best"] is False


def test_unrelated_spools_are_not_suggested():
    spool = {"id": 1, "filament": {"name": "Pink", "material": "PLA", "color_hex": "FF6762", "vendor": {"name": "eSun"}}}
    ranked = rank_spools_for_link(loaded_spool(), [spool])
    assert (ranked[0]["suggested"], ranked[0]["best"], ranked[0]["confidence"]) == (False, False, "Low")


def test_the_same_material_uuid_counts_for_a_lot():
    filament = {"name": "Other", "material": "PLA", "extra": {"opt_material_uuid": json.dumps(MATERIAL_UUID)}, "vendor": {"name": "eSun"}}
    assert rank_spools_for_link(loaded_spool(), [{"id": 1, "filament": filament}])[0]["score"] >= 40


def test_a_spool_at_another_printer_scores_lower():
    at_printer = {**SPOOL_52, "location": "Office"}
    assert score_spool(loaded_spool(), at_printer, ("Office",))["score"] < score_spool(loaded_spool(), at_printer)["score"]


def test_linked_and_archived_spools_are_not_candidates():
    spools = [{**SPOOL_52, "id": 1, "archived": True}, {**SPOOL_52, "id": 2, "extra": {"opt_instance_uuid": json.dumps("taken")}}]
    assert rank_spools_for_link(loaded_spool(), spools) == []


def test_changes_show_before_and_after():
    changes = {c["key"]: c for c in build_link_changes(loaded_spool(), SPOOL_52, 0)}
    assert (changes["spool_initial_weight"]["before"], changes["spool_initial_weight"]["after"]) == (1000.0, 959)
    assert (changes["filament_color_hex"]["before"], changes["filament_color_hex"]["after"]) == ("24292A", "212929")
    assert changes["spool_instance"]["locked"] is True
    assert all(c["checked"] for c in changes.values())
    assert "filament_weight" not in changes


@pytest.mark.parametrize(
    "used,checked,noted",
    [(120.0, False, True), (31.5, False, True), (31.0, True, False), (0.0, True, False)],
)
def test_more_usage_in_spoolman_keeps_its_value_by_default(used, checked, noted):
    changes = {c["key"]: c for c in build_link_changes(loaded_spool(30), {**SPOOL_52, "used_weight": used}, 0)}
    row = changes["spool_used_weight"]
    assert row["checked"] is checked
    assert bool(row["note"]) is noted
    assert changes["spool_initial_weight"]["checked"] is True


def test_filament_changes_are_off_when_the_filament_is_shared():
    changes = build_link_changes(loaded_spool(), SPOOL_52, 2)
    assert all(c["checked"] is False for c in changes if c["target"] == "filament" and c["key"] != "filament_material")
    assert next(c for c in changes if c["key"] == "filament_material")["checked"] is True
    assert all(c["checked"] is True for c in changes if c["target"] == "spool")


@respx.mock(assert_all_called=False)
def test_link_applies_the_selected_changes_and_keeps_extras(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/52").respond(json=SPOOL_52)
    spool_patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/52").respond(json={})
    filament_patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/filament/49").respond(json={})
    make_bridge(tmp_path, printer(), auto_create=False).bridge.link_spool(loaded_spool(), 52, ALL_KEYS)
    body = json.loads(spool_patch.calls[0].request.content)
    assert body["extra"] == {"card_uids": json.dumps("ABC"), "opt_instance_uuid": json.dumps(UUID), "opt_consumed_weight": "30.0"}
    assert (body["initial_weight"], body["spool_weight"], body["used_weight"]) == (959, 277, 30)
    assert "location" not in body
    filament = json.loads(filament_patch.calls[0].request.content)
    assert (filament["name"], filament["color_hex"], filament["spool_weight"]) == ("PC Blend Jet Black", "212929", 277)
    assert filament["extra"] == {"variant": json.dumps("x"), "opt_material_uuid": json.dumps(MATERIAL_UUID)}
    assert json.loads((tmp_path / "state.json").read_text())["consumed"][UUID] == 30


@respx.mock(assert_all_called=False)
def test_link_with_nothing_selected_only_sets_the_link(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/52").respond(json=SPOOL_52)
    spool_patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/52").respond(json={})
    filament_patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/filament/49").respond(json={})
    make_bridge(tmp_path, printer(), auto_create=False).bridge.link_spool(loaded_spool(), 52, set())
    assert set(json.loads(spool_patch.calls[0].request.content)) == {"extra"}
    assert not filament_patch.called


@respx.mock(assert_all_called=False)
def test_link_refuses_a_spool_linked_to_another_instance(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/52").respond(json={"id": 52, "extra": {"opt_instance_uuid": json.dumps("other")}})
    with pytest.raises(Exception, match="already linked"):
        make_bridge(tmp_path, printer(), auto_create=False).bridge.link_spool(loaded_spool(), 52, set())
    assert writes(respx_mock) == []


@respx.mock(assert_all_called=False)
def test_unlink_keeps_other_extras_and_notices_if_the_link_stays(respx_mock, tmp_path):
    linked = {"id": 52, "extra": {"card_uids": json.dumps("ABC"), "opt_instance_uuid": json.dumps(UUID)}}
    unlinked = {"id": 52, "extra": {"card_uids": json.dumps("ABC")}}
    route = respx_mock.get(f"{SPOOLMAN}/api/v1/spool/52")
    route.side_effect = [httpx.Response(200, json=linked), httpx.Response(200, json=unlinked)]
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/52").respond(json={})
    bridge = make_bridge(tmp_path, printer(), auto_create=False).bridge
    bridge.unlink_spool(52)
    assert json.loads(patch.calls[0].request.content) == {"extra": {"card_uids": json.dumps("ABC"), "opt_instance_uuid": None, "opt_consumed_weight": None}}
    route.side_effect = [httpx.Response(200, json=linked), httpx.Response(200, json=linked)]
    with pytest.raises(Exception, match="kept the link"):
        bridge.unlink_spool(52)


def test_a_stale_link_can_only_be_replaced_without_nfc():
    custom = parse_loaded_spools(custom_tag_printer())[0]
    tagged = loaded_spool()
    assert can_replace_link(custom, "old-load", {"custom"}) is True
    assert can_replace_link(custom, "on-another-printer", {"custom", "on-another-printer"}) is False
    assert can_replace_link(tagged, "old-load", {UUID}) is False
    linked = {**SPOOL_52, "extra": {"opt_instance_uuid": json.dumps("old-load")}}
    assert [c["id"] for c in rank_spools_for_link(custom, [linked], loaded_now={"custom"})] == [52]
    assert rank_spools_for_link(tagged, [linked], loaded_now={UUID}) == []


# Spools from the Warehouse in the Prusa app


def warehouse_spool_printer(consumed=6):
    data = custom_tag_printer(consumed=consumed)
    instance = data["fff"]["tools"][0]["materialPackageInstance"]
    instance["isFromNfc"] = True
    return data


WAREHOUSE_ITEM = {
    "name": "PETG-GF Black",
    "consumedWeight": 6,
    "nominalNettoFullWeight": 1000,
    "materialPackageInstance": {
        "uuid": "custom",
        "actualNettoFullWeight": None,
        "package": {
            "filamentDiameter": 1.75,
            "container": {"emptyWeight": 220},
            "material": {
                "uuid": MATERIAL_UUID,
                "type": "PETG",
                "primaryColor": {"colorRgba": "#101010"},
                "properties": {"density": 1.35},
                "brand": {"name": "ELEGOO"},
            },
        },
    },
}


def test_warehouse_details_fill_only_what_the_tool_did_not_report():
    loaded = parse_loaded_spools(warehouse_spool_printer())[0]
    filled = fill_from_warehouse(loaded, WAREHOUSE_ITEM)
    assert filled.display_name == "ELEGOO PETG-GF Black"
    assert (filled.empty_weight, filled.density, filled.material_type) == (220, 1.35, "PETG")
    assert filled.can_create is True
    other = {**WAREHOUSE_ITEM, "consumedWeight": 99}
    assert fill_from_warehouse(loaded, other).consumed_weight == 6


def test_a_warehouse_spool_uses_the_nominal_weight_as_its_full_weight():
    loaded = parse_loaded_spools(warehouse_spool_printer(consumed=250))[0]
    assert loaded.actual_weight is None
    assert loaded.full_weight == 1000
    assert loaded.remaining_fraction == 0.75


def test_a_nominal_weight_does_not_replace_spoolmans_weight_by_default():
    loaded = parse_loaded_spools(warehouse_spool_printer())[0]
    spool = {"id": 55, "initial_weight": 1010, "filament": {}, "extra": {}}
    change = next(c for c in build_link_changes(loaded, spool, 0) if c["key"] == "spool_initial_weight")
    assert (change["after"], change["checked"]) == (1000, False)


@respx.mock(assert_all_called=False)
def test_poll_adds_warehouse_details_to_a_loaded_spool(respx_mock, tmp_path):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[{"id": 55, "location": "Core One", "extra": {"opt_instance_uuid": json.dumps("custom")}}])
    respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/55").respond(json={})
    poller = make_bridge(tmp_path, warehouse_spool_printer())
    poller.bridge.connect.warehouse = [WAREHOUSE_ITEM, {"name": "Other", "materialPackageInstance": None}]
    assert poller.poll()["tools"][0]["spool"].display_name == "ELEGOO PETG-GF Black"


@respx.mock(assert_all_called=False)
def test_consumption_is_still_tracked_when_the_warehouse_cannot_be_read(respx_mock, tmp_path):
    (tmp_path / "state.json").write_text(json.dumps({"consumed": {"custom": 3}}))
    mock_fields(respx_mock)
    spool = {"id": 55, "location": "Core One", "used_weight": 0, "extra": {"opt_instance_uuid": json.dumps("custom")}}
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[spool])
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/55").respond(json=spool)
    use = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/55").respond(json={})
    poller = make_bridge(tmp_path, warehouse_spool_printer(consumed=8))
    poller.bridge.connect.warehouse = ConnectError("no warehouse")
    assert poller.poll()["tools"][0]["spool"].brand is None
    assert json.loads(use.calls[0].request.content)["used_weight"] == 5


def test_a_spool_with_full_tag_data_does_not_read_the_warehouse():
    connect = FakeConnect(printer(), warehouse=AssertionError("should not be read"))
    bridge = Bridge(connect, None, None)
    assert bridge._with_warehouse_details(parse_loaded_spools(printer()), "org-1")[0].brand == "Prusament"


@respx.mock(assert_all_called=False)
def test_warehouse_items_follows_every_page(respx_mock, tmp_path):
    s = settings(tmp_path)
    store = TokenStore(tmp_path / "tokens.json")
    store.save(Tokens(make_jwt(time.time() + 3600), "refresh"))
    pages = [
        {"pageInfo": {"hasNextPage": True, "endCursor": "next"}, "nodes": [{"name": "A"}]},
        {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{"name": "B"}]},
    ]
    route = respx_mock.post(GRAPHQL)
    route.side_effect = [httpx.Response(200, json={"data": {"warehouse": {"items": page}}}) for page in pages]
    items = ConnectClient(s, store, httpx.Client()).warehouse_items("org-1")
    assert [item["name"] for item in items] == ["A", "B"]
    assert json.loads(route.calls[1].request.content)["variables"]["after"] == "next"


def test_remaining_weight_uses_the_measured_weight_when_it_is_known():
    assert parse_loaded_spools(printer(consumed=59))[0].remaining_weight == 900
    assert parse_loaded_spools(warehouse_spool_printer())[0].remaining_weight == 994


def test_only_a_spool_that_used_the_warehouse_is_marked_as_a_warehouse_spool():
    loaded = parse_loaded_spools(warehouse_spool_printer())[0]
    assert loaded.from_warehouse is False
    assert fill_from_warehouse(loaded, WAREHOUSE_ITEM).from_warehouse is True


@respx.mock(assert_all_called=False)
def test_unlink_falls_back_for_a_spoolman_that_replaces_extras(respx_mock, tmp_path):
    linked = {"id": 52, "extra": {"card_uids": json.dumps("ABC"), "opt_instance_uuid": json.dumps(UUID)}}
    unlinked = {"id": 52, "extra": {"card_uids": json.dumps("ABC")}}
    route = respx_mock.get(f"{SPOOLMAN}/api/v1/spool/52")
    route.side_effect = [httpx.Response(200, json=linked), httpx.Response(200, json=unlinked)]
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/52")
    patch.side_effect = [httpx.Response(422, json={"detail": "null"}), httpx.Response(200, json={})]
    make_bridge(tmp_path, printer(), auto_create=False).bridge.unlink_spool(52)
    assert json.loads(patch.calls[1].request.content) == {"extra": {"card_uids": json.dumps("ABC")}}


# Duplicates


def scanned_spool(spool_id=60, tags=({"uid": "E004010", "format": "openprinttag"},)):
    """A spool Spoolman created itself when its own reader scanned the tag: it has no link to the instance."""
    return {
        "id": spool_id,
        "archived": False,
        "location": None,
        "used_weight": 0,
        "remaining_weight": 959,
        "extra": {},
        "tags": list(tags),
        "filament": {"id": 49, "name": "PC Blend Jet Black", "material": "PC", "color_hex": "212929", "weight": 1000, "vendor": {"name": "Prusament"}, "extra": {}},
    }


@respx.mock(assert_all_called=False)
def test_auto_create_does_not_duplicate_a_spool_spoolman_already_has(respx_mock, tmp_path, caplog):
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[scanned_spool()])
    result = make_bridge(tmp_path, printer(consumed=30)).poll()
    assert writes(respx_mock) == []
    assert result["tools"][0]["spool_id"] is None
    assert "#60" in result["warnings"][0] and "Has an OpenPrintTag tag in Spoolman" in result["warnings"][0]
    assert "Did not create a new spool" in caplog.text


@respx.mock(assert_all_called=False)
def test_auto_create_still_creates_when_nothing_looks_like_the_spool(respx_mock, tmp_path):
    other = scanned_spool()
    other["filament"] = {"id": 3, "name": "Galaxy Black", "material": "PLA", "color_hex": "FFFFFF", "vendor": {"name": "Other"}, "extra": {}}
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[other])
    respx_mock.get(f"{SPOOLMAN}/api/v1/vendor").respond(json=[{"id": 7, "name": "Prusament"}])
    respx_mock.get(f"{SPOOLMAN}/api/v1/filament").respond(json=[{"id": 8, "name": "PC Blend Jet Black", "vendor": {"id": 7}}])
    created = respx_mock.post(f"{SPOOLMAN}/api/v1/spool").respond(json={"id": 9})
    make_bridge(tmp_path, printer()).poll()
    assert created.called


@respx.mock(assert_all_called=False)
def test_two_spools_linked_to_the_loaded_tag_are_reported(respx_mock, tmp_path, caplog):
    second = {**existing_spool(), "id": 43}
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[existing_spool(), second])
    poller = make_bridge(tmp_path, printer())
    result = poller.poll()
    poller.poll()
    assert result["warnings"] == [f"Spools #42, #43 are all linked to the loaded tag {UUID}. Only #42 gets the consumption. Unlink the duplicates."]
    assert result["tools"][0]["spool_id"] == 42
    assert caplog.text.count("are all linked to instance") == 1


def test_an_openprinttag_tag_is_shown_as_a_reason():
    candidate = score_spool(loaded_spool(), scanned_spool())
    assert "Has an OpenPrintTag tag in Spoolman" in candidate["reasons"]
    assert candidate["confidence"] == "High"


def test_warehouse_items_become_spools_without_a_printer():
    items = [
        {**WAREHOUSE_ITEM, "location": "Shelf A", "materialPackageInstanceUuidSource": "generated"},
        {**WAREHOUSE_ITEM, "archived": True},
        {**WAREHOUSE_ITEM, "empty": True},
        {"name": "No instance", "materialPackageInstance": None},
    ]
    [entry] = parse_warehouse_items(items)
    assert entry["location"] == "Shelf A"
    spool = entry["spool"]
    assert (spool.uuid, spool.tool_id, spool.is_from_nfc, spool.from_warehouse) == ("custom", "", False, True)
    assert spool.display_name == "ELEGOO PETG-GF Black"
    assert spool.full_weight == 1000


def test_a_warehouse_spool_read_from_a_tag_is_marked_as_nfc():
    [entry] = parse_warehouse_items([{**WAREHOUSE_ITEM, "materialPackageInstanceUuidSource": "nfc"}])
    assert entry["spool"].is_from_nfc is True


def test_a_warehouse_spool_never_takes_over_another_spools_link():
    [entry] = parse_warehouse_items([WAREHOUSE_ITEM])
    assert can_replace_link(entry["spool"], "another-spool", set()) is False
    taken = {"id": 7, "filament": {}, "extra": {"opt_instance_uuid": json.dumps("another-spool")}}
    free = {"id": 8, "filament": {}, "extra": {}}
    assert [c["id"] for c in rank_spools_for_link(entry["spool"], [taken, free])] == [8]


def warehouse_link_change(filament, shared=0, item=WAREHOUSE_ITEM):
    [entry] = parse_warehouse_items([item])
    spool = {"id": 55, "filament": filament, "extra": {}}
    return next((c for c in build_link_changes(entry["spool"], spool, shared) if c["key"] == "filament_material"), None)


def test_linking_offers_a_missing_material_uuid_by_default():
    assert warehouse_link_change({"id": 1, "extra": {}}, shared=3)["checked"] is True
    assert warehouse_link_change({"id": 1, "extra": {}}, shared=0)["checked"] is True


def test_linking_does_not_replace_another_material_uuid_on_a_shared_filament_by_default():
    filament = {"id": 1, "extra": {"opt_material_uuid": json.dumps("other")}}
    assert warehouse_link_change(filament, shared=3)["checked"] is False


def test_linking_offers_nothing_when_the_material_uuid_already_matches():
    assert warehouse_link_change({"id": 1, "extra": {"opt_material_uuid": json.dumps(MATERIAL_UUID)}}) is None


def creation_mocks(mock, filaments, vendors=()):
    mock_fields(mock)
    mock.get(f"{SPOOLMAN}/api/v1/vendor").respond(json=list(vendors))
    mock.get(f"{SPOOLMAN}/api/v1/filament").respond(json=filaments)
    return mock.post(f"{SPOOLMAN}/api/v1/spool").respond(json={"id": 60})


def warehouse_spool():
    return parse_warehouse_items([WAREHOUSE_ITEM])[0]["spool"]


@respx.mock(assert_all_called=False)
def test_creating_reuses_the_filament_with_the_same_material_uuid(respx_mock, tmp_path):
    other = {"id": 8, "name": "Another name", "vendor": {"id": 3, "name": "Another vendor"}, "extra": {"opt_material_uuid": json.dumps(MATERIAL_UUID)}}
    spool = creation_mocks(respx_mock, [{"id": 9, "name": "PETG-GF Black", "vendor": {"id": 4}, "extra": {}}, other], [{"id": 4, "name": "ELEGOO"}])
    make_bridge(tmp_path, printer()).bridge.create_spool(warehouse_spool(), "")
    body = json.loads(spool.calls[0].request.content)
    assert body["filament_id"] == 8
    assert body["location"] is None
    assert body["initial_weight"] == 1000
    assert len(writes(respx_mock)) == 1


@respx.mock(assert_all_called=False)
def test_creating_falls_back_to_brand_and_name(respx_mock, tmp_path):
    spool = creation_mocks(respx_mock, [{"id": 9, "name": "petg-gf black", "vendor": {"id": 4}, "extra": {}}], [{"id": 4, "name": "Elegoo"}])
    make_bridge(tmp_path, printer()).bridge.create_spool(warehouse_spool(), "Shelf")
    assert json.loads(spool.calls[0].request.content)["filament_id"] == 9
    assert len(writes(respx_mock)) == 1


@respx.mock(assert_all_called=False)
def test_a_new_filament_gets_the_material_uuid(respx_mock, tmp_path):
    creation_mocks(respx_mock, [], [{"id": 4, "name": "ELEGOO"}])
    created = respx_mock.post(f"{SPOOLMAN}/api/v1/filament").respond(json={"id": 12})
    make_bridge(tmp_path, printer()).bridge.create_spool(warehouse_spool(), "")
    body = json.loads(created.calls[0].request.content)
    assert extra_value(body, "opt_material_uuid") == MATERIAL_UUID
    assert (body["name"], body["material"], body["density"], body["spool_weight"]) == ("PETG-GF Black", "PETG", 1.35, 220)


@respx.mock(assert_all_called=False)
def test_the_preview_finds_the_filament_by_material_uuid(respx_mock, tmp_path):
    other = {"id": 8, "name": "Another name", "vendor": {"id": 3}, "extra": {"opt_material_uuid": json.dumps(MATERIAL_UUID)}}
    creation_mocks(respx_mock, [other])
    preview = make_bridge(tmp_path, printer()).bridge.create_preview(warehouse_spool())
    assert preview == {"vendor_exists": True, "filament_exists": True}


def test_warehouse_interval_setting(tmp_path):
    assert load_settings({"DATA_DIR": str(tmp_path)}).warehouse_interval == 600
    assert load_settings({"DATA_DIR": str(tmp_path), "WAREHOUSE_INTERVAL": "120"}).warehouse_interval == 120
    with pytest.raises(ConfigError):
        load_settings({"DATA_DIR": str(tmp_path), "WAREHOUSE_INTERVAL": "5"})


@respx.mock(assert_all_called=False)
def test_a_spool_created_from_the_warehouse_only_deducts_what_was_used_after(respx_mock, tmp_path):
    """Connect had already counted 6 g when the spool was created, so a later reading of 10 g deducts 4 g."""
    created = creation_mocks(respx_mock, [], [{"id": 4, "name": "ELEGOO"}])
    respx_mock.post(f"{SPOOLMAN}/api/v1/filament").respond(json={"id": 12})
    make_bridge(tmp_path, printer()).bridge.create_spool(warehouse_spool(), "")
    body = json.loads(created.calls[0].request.content)
    assert body["used_weight"] == 6
    assert json.loads(body["extra"]["opt_consumed_weight"]) == 6

    spool = {"id": 42, "location": "Core One", "archived": False, "used_weight": 6.0,
             "first_used": "2026-10-01T10:00:00Z", "extra": body["extra"]}
    respx_mock.reset()
    mock_fields(respx_mock)
    patch = mock_deduction(respx_mock, spool)
    make_bridge(tmp_path, warehouse_spool_printer(consumed=10)).poll()
    deductions = [json.loads(c.request.content) for c in patch.calls if "used_weight" in json.loads(c.request.content)]
    assert [d["used_weight"] for d in deductions] == [10.0]


def test_empty_warehouse_spools_are_kept_only_when_asked():
    item = {**WAREHOUSE_ITEM, "empty": True}
    assert parse_warehouse_items([item]) == []
    [entry] = parse_warehouse_items([item], include_empty=True)
    assert entry["empty"] is True
    assert parse_warehouse_items([{**WAREHOUSE_ITEM, "archived": True}], include_empty=True) == []
