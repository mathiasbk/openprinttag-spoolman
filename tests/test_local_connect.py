import json
import time
from dataclasses import replace

import httpx
import pytest
import respx

from bridge.config import ConfigError, PrinterConfig, load_settings, read_config, update_config
from bridge.connect import ConnectAuthError, ConnectError
from bridge.local_connect import MATERIALS_NAMESPACE, LocalConnectClient
from bridge.service import Service
from bridge.spoolman import SpoolmanClient
from bridge.sync import Bridge, State, parse_loaded_spools, parse_warehouse_items
from tests.test_bridge import GRAPHQL, SPOOLMAN, make_jwt, UUID, existing_spool, linked_extra, mock_deduction, mock_fields, settings
from tests.test_web import graphql, make_service, signed_in

LOCAL = "http://connect.local:8080"
BASE = f"{LOCAL}/api/v1/modules/{MATERIALS_NAMESPACE}"
TOKEN = "pcl_secret"
MATERIAL = "517f24cc-16a4-5091-8325-5f6873e9dfcb"
TAGLESS = "8b0c7f3e-0000-4000-8000-000000000001"
LOCAL_PRINTER = PrinterConfig(uuid="dev-1", organization_id="local", name="Core One", location="Core One")


def local_settings(tmp_path, **more):
    return replace(settings(tmp_path), **{"connect_source": "local", "local_connect_url": LOCAL, "local_connect_token": TOKEN, **more})


def spool(id=7, uuid=UUID, source="printer", remaining=880.0, **more):
    return {
        "id": id,
        "uuid": uuid,
        "source": source,
        "material_uuid": MATERIAL,
        "material_name": "Prusament PC Blend Jet Black",
        "brand_name": "Prusament",
        "type": "PC",
        "color_rgba": "#212929ff",
        "density": 1.22,
        "diameter_um": 1750,
        "full_weight_g": 1000.0,
        "empty_weight_g": 201.0,
        "remaining_weight_g": remaining,
        "location": "Shelf",
        "archived_at": None,
        **more,
    }


def tool_row(tool_id="0", spool_id=7, spool_uuid=UUID, remaining=880.0, from_nfc=True, device="dev-1"):
    row = {
        "device_id": device,
        "tool_id": tool_id,
        "material_uuid": MATERIAL,
        "material_name": "Prusament PC Blend Jet Black",
        "brand_name": "Prusament",
        "type": "PC",
        "abbreviation": "PC",
        "color_rgba": "#212929ff",
        "by": "printer" if from_nfc else "person",
        "from_nfc": from_nfc,
    }
    if spool_id is not None:
        row.update(spool_id=spool_id, spool_uuid=spool_uuid, remaining_weight_g=remaining, full_weight_g=1000.0)
    return row


def mock_local(mock, tools, spools, printers=None):
    printers = printers if printers is not None else [{"id": "dev-1", "name": "Core One", "state": "IDLE"}]
    mock.get(f"{BASE}/printers").respond(json={"printers": printers})
    mock.get(f"{BASE}/printers/tools").respond(json={"tools": tools, "total": 1})
    return mock.get(f"{BASE}/spools").respond(json={"spools": spools, "total": len(spools)})


@respx.mock(assert_all_called=False)
def test_a_tagged_spool_on_a_tool_reads_like_one_from_the_cloud(respx_mock, tmp_path):
    mock_local(respx_mock, [tool_row()], [spool()])
    printer = LocalConnectClient(local_settings(tmp_path), httpx.Client()).fetch_printer("local", "dev-1")
    assert printer["name"] == "Core One" and printer["state"] == "IDLE"
    [loaded] = parse_loaded_spools(printer)
    assert loaded.uuid == UUID and loaded.tool_id == "0" and loaded.is_from_nfc
    assert loaded.consumed_weight == 120.0
    assert loaded.full_weight == 1000.0 and loaded.remaining_weight == 880.0
    assert (loaded.brand, loaded.material_name, loaded.material_type) == ("Prusament", "Prusament PC Blend Jet Black", "PC")
    assert (loaded.density, loaded.filament_diameter, loaded.empty_weight) == (1.22, 1.75, 201.0)
    assert loaded.color_hex == "212929" and loaded.material_uuid == MATERIAL
    assert loaded.can_create
    request = respx_mock.calls[0].request
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"


@respx.mock(assert_all_called=False)
def test_a_spool_without_a_tag_is_reported_with_its_own_uuid(respx_mock, tmp_path):
    mock_local(respx_mock, [tool_row(spool_uuid=TAGLESS, from_nfc=False)], [spool(uuid=TAGLESS, source="manual")])
    [loaded] = parse_loaded_spools(LocalConnectClient(local_settings(tmp_path), httpx.Client()).fetch_printer("local", "dev-1"))
    assert loaded.uuid == TAGLESS and not loaded.is_from_nfc and loaded.consumed_weight == 120.0


@respx.mock(assert_all_called=False)
def test_tools_without_a_spool_and_other_printers_are_left_out(respx_mock, tmp_path):
    mock_local(respx_mock, [tool_row(spool_id=None, from_nfc=False), tool_row(tool_id="1", device="dev-2")], [])
    printer = LocalConnectClient(local_settings(tmp_path), httpx.Client()).fetch_printer("local", "dev-1")
    assert len(printer["fff"]["tools"]) == 1
    assert parse_loaded_spools(printer) == []


@respx.mock(assert_all_called=False)
def test_a_tagged_spool_not_yet_in_materials_is_added_and_read(respx_mock, tmp_path):
    mock_local(respx_mock, [tool_row(spool_id=None)], [spool()])
    add = respx_mock.post(f"{BASE}/printers/dev-1/tools/0/spool").respond(json=spool())
    [loaded] = parse_loaded_spools(LocalConnectClient(local_settings(tmp_path), httpx.Client()).fetch_printer("local", "dev-1"))
    assert add.called and add.calls[0].request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert loaded.uuid == UUID and loaded.is_from_nfc and loaded.consumed_weight == 120.0


@respx.mock(assert_all_called=False)
def test_a_refused_spool_add_leaves_the_tool_empty(respx_mock, tmp_path):
    mock_local(respx_mock, [tool_row(spool_id=None)], [])
    respx_mock.post(f"{BASE}/printers/dev-1/tools/0/spool").respond(409, json={"code": "materials/NOT_REPORTED", "title": "No tag"})
    printer = LocalConnectClient(local_settings(tmp_path), httpx.Client()).fetch_printer("local", "dev-1")
    assert parse_loaded_spools(printer) == []


@respx.mock(assert_all_called=False)
def test_a_spool_already_in_materials_is_not_added_again(respx_mock, tmp_path):
    mock_local(respx_mock, [tool_row()], [spool()])
    add = respx_mock.post(f"{BASE}/printers/dev-1/tools/0/spool").respond(json=spool())
    LocalConnectClient(local_settings(tmp_path), httpx.Client()).fetch_printer("local", "dev-1")
    assert not add.called


@respx.mock(assert_all_called=False)
def test_a_printer_the_server_does_not_list_is_an_error(respx_mock, tmp_path):
    mock_local(respx_mock, [], [], printers=[])
    with pytest.raises(ConnectError, match="does not list dev-1"):
        LocalConnectClient(local_settings(tmp_path), httpx.Client()).fetch_printer("local", "dev-1")


@respx.mock(assert_all_called=False)
def test_the_spools_are_the_warehouse(respx_mock, tmp_path):
    spools = [
        spool(),
        spool(id=8, uuid=TAGLESS, source="manual", remaining=0.0),
        spool(id=9, uuid="archived-1", archived_at="2026-11-10T10:00:00Z"),
    ]
    mock_local(respx_mock, [], spools)
    items = LocalConnectClient(local_settings(tmp_path), httpx.Client()).warehouse_items("local")
    entries = parse_warehouse_items(items, include_empty=True)
    assert [(e["spool"].uuid, e["empty"], e["location"]) for e in entries] == [(UUID, False, "Shelf"), (TAGLESS, True, "Shelf")]
    tagged = entries[0]["spool"]
    assert tagged.is_from_nfc and tagged.consumed_weight == 120.0 and tagged.remaining_weight == 880.0
    assert (tagged.brand, tagged.material_type, tagged.density, tagged.filament_diameter) == ("Prusament", "PC", 1.22, 1.75)
    assert not entries[1]["spool"].is_from_nfc


@respx.mock(assert_all_called=False)
def test_spools_are_read_page_by_page(respx_mock, tmp_path):
    route = respx_mock.get(f"{BASE}/spools")
    route.side_effect = [
        httpx.Response(200, json={"spools": [spool()], "total": 2, "next": 100}),
        httpx.Response(200, json={"spools": [spool(id=8, uuid=TAGLESS)], "total": 2}),
    ]
    items = LocalConnectClient(local_settings(tmp_path), httpx.Client()).warehouse_items("local")
    assert len(items) == 2
    assert [c.request.url.params["offset"] for c in route.calls] == ["0", "100"]


@respx.mock(assert_all_called=False)
def test_a_refused_credential_asks_for_a_new_one(respx_mock, tmp_path):
    respx_mock.get(f"{BASE}/printers").respond(401, json={"code": "auth/unauthenticated", "title": "Sign in first"})
    with pytest.raises(ConnectAuthError, match="Sign in first"):
        LocalConnectClient(local_settings(tmp_path), httpx.Client()).me()


@respx.mock(assert_all_called=False)
def test_a_missing_materials_extension_is_named(respx_mock, tmp_path):
    respx_mock.get(f"{BASE}/printers").respond(404)
    with pytest.raises(ConnectError, match="Materials extension"):
        LocalConnectClient(local_settings(tmp_path), httpx.Client()).list_printers()


def test_no_request_is_made_without_a_credential(tmp_path):
    client = LocalConnectClient(local_settings(tmp_path, local_connect_token=""), httpx.Client())
    with pytest.raises(ConnectAuthError):
        client.me()


@respx.mock(assert_all_called=False)
def test_consumption_from_the_self_hosted_server_is_deducted_in_spoolman(respx_mock, tmp_path):
    mock_local(respx_mock, [tool_row(remaining=870.0)], [spool(remaining=870.0)])
    mock_fields(respx_mock)
    patch = mock_deduction(respx_mock, existing_spool(extra=linked_extra(120)))
    s = local_settings(tmp_path)
    bridge = Bridge(LocalConnectClient(s, httpx.Client()), SpoolmanClient(s, httpx.Client()), State(s.state_path, False))
    bridge.poll(LOCAL_PRINTER)
    body = json.loads(patch.calls[0].request.content)
    assert body["used_weight"] == 110.0
    assert body["extra"] == linked_extra(130.0)


def test_the_source_is_read_from_the_config(tmp_path):
    update_config(tmp_path, {"connect_source": "local", "local_connect_url": LOCAL + "/", "local_connect_token": TOKEN})
    loaded = load_settings({"DATA_DIR": str(tmp_path)})
    assert loaded.is_local and loaded.local_connect_url == LOCAL and loaded.local_connect_token == TOKEN
    assert not load_settings({"DATA_DIR": str(tmp_path / "other")}).is_local
    with pytest.raises(ConfigError):
        load_settings({"DATA_DIR": str(tmp_path / "other"), "CONNECT_SOURCE": "moon"})


def test_the_service_is_ready_with_an_address_and_a_credential(tmp_path):
    service = Service({"DATA_DIR": str(tmp_path), "SPOOLMAN_URL": SPOOLMAN, "CONNECT_SOURCE": "local"})
    assert isinstance(service.connect, LocalConnectClient)
    assert not service.connect_ready()
    service = Service(
        {"DATA_DIR": str(tmp_path), "SPOOLMAN_URL": SPOOLMAN, "CONNECT_SOURCE": "local", "LOCAL_CONNECT_URL": LOCAL, "LOCAL_CONNECT_TOKEN": TOKEN}
    )
    assert service.connect_ready()
    assert service.token_status()["local"] and TOKEN not in json.dumps(service.token_status())


@respx.mock(assert_all_called=False)
def test_the_setup_page_switches_to_the_self_hosted_server(respx_mock, tmp_path):
    respx_mock.get(f"{BASE}/printers").respond(json={"printers": [{"id": "dev-1", "name": "Core One", "state": "IDLE"}]})
    service = Service({"DATA_DIR": str(tmp_path), "SPOOLMAN_URL": SPOOLMAN})
    update_config(tmp_path, {"printers": [{"uuid": "cloud-1", "organization_id": "org-1", "name": "Core"}]})
    service.reload()
    admin = signed_in(service)
    page = admin.get("/setup/connect?source=local")
    assert "Self-hosted Connect" in page.text and "API credential" in page.text

    tested = admin.post("/setup/connect", data={"source": "local", "local_connect_url": LOCAL, "local_connect_token": TOKEN, "action": "test", "verify_tls": "on"})
    assert "Found 1 printer." in tested.text
    assert "connect_source" not in read_config(tmp_path)

    saved = admin.post("/setup/connect", data={"source": "local", "local_connect_url": LOCAL, "local_connect_token": TOKEN, "action": "save", "verify_tls": "on"})
    assert saved.status_code == 302 and saved.headers["Location"].endswith("/setup/printers")
    config = read_config(tmp_path)
    assert config["connect_source"] == "local" and config["local_connect_token"] == TOKEN
    assert config["printers"] == []
    assert service.settings.is_local and isinstance(service.connect, LocalConnectClient)

    again = admin.get("/setup/connect")
    assert TOKEN not in again.text and "Stored. Leave empty to keep it" in again.text
    printers = admin.get("/setup/printers")
    assert "Core One" in printers.text


@respx.mock(assert_all_called=False)
def test_a_bad_address_is_not_saved(respx_mock, tmp_path):
    service = Service({"DATA_DIR": str(tmp_path), "SPOOLMAN_URL": SPOOLMAN})
    admin = signed_in(service)
    response = admin.post("/setup/connect", data={"source": "local", "local_connect_url": "connect.local", "local_connect_token": TOKEN})
    assert "Enter the address" in response.text
    assert "connect_source" not in read_config(tmp_path)


@respx.mock(assert_all_called=False)
def test_a_cloud_login_switches_back_from_the_self_hosted_server(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    update_config(
        tmp_path,
        {"connect_source": "local", "local_connect_url": LOCAL, "local_connect_token": TOKEN, "printers": [{"uuid": "dev-1", "organization_id": "local"}]},
    )
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    assert service.settings.is_local
    service.log_in_to_connect(make_jwt(time.time() + 3600), "refresh")
    assert not service.settings.is_local and service.settings.printers == ()
    assert service.connect_ready()


@respx.mock(assert_all_called=False)
def test_an_unticked_certificate_check_is_saved_as_off(respx_mock, tmp_path):
    respx_mock.get(f"{BASE}/printers").respond(json={"printers": []})
    service = Service({"DATA_DIR": str(tmp_path), "SPOOLMAN_URL": SPOOLMAN})
    admin = signed_in(service)
    admin.post("/setup/connect", data={"source": "local", "local_connect_url": LOCAL, "local_connect_token": TOKEN})
    assert read_config(tmp_path)["local_connect_verify_tls"] is False


@respx.mock(assert_all_called=False)
def test_a_credential_that_may_not_add_warns_once_and_names_the_scope(respx_mock, tmp_path, caplog):
    mock_local(respx_mock, [tool_row(spool_id=None)], [])
    add = respx_mock.post(f"{BASE}/printers/dev-1/tools/0/spool").respond(403, json={"code": "core.auth/ACCESS_DENIED"})
    client = LocalConnectClient(local_settings(tmp_path), httpx.Client())
    with caplog.at_level("DEBUG", logger="bridge.local_connect"):
        for _ in range(3):
            assert parse_loaded_spools(client.fetch_printer("local", "dev-1")) == []
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert add.call_count == 3
    assert len(warnings) == 1 and "operate scope" in warnings[0].getMessage()


@respx.mock(assert_all_called=False)
def test_a_tool_warns_again_after_an_add_succeeded(respx_mock, tmp_path, caplog):
    mock_local(respx_mock, [tool_row(spool_id=None)], [spool()])
    add = respx_mock.post(f"{BASE}/printers/dev-1/tools/0/spool")
    add.side_effect = [
        httpx.Response(409, json={"code": "materials/NOT_REPORTED"}),
        httpx.Response(200, json=spool()),
        httpx.Response(409, json={"code": "materials/NOT_REPORTED"}),
    ]
    client = LocalConnectClient(local_settings(tmp_path), httpx.Client())
    with caplog.at_level("WARNING", logger="bridge.local_connect"):
        for _ in range(3):
            client.fetch_printer("local", "dev-1")
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 2
