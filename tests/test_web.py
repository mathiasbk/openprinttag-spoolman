import base64
import json
import os
import re
import stat
import time

import pytest
import respx

from bridge.config import read_config
from bridge.connect import Tokens
from bridge.service import Service
from bridge.spoolman import extra_value
from bridge.web import LoginNonces, bookmarklet, create_app
from tests.test_bridge import GRAPHQL, SPOOLMAN, UUID, make_jwt, mock_fields, printer

PASSWORD = "correct-password"
ME = {"data": {"authorization": {"me": {"email": "a@example.com", "organizations": [{"id": "org-1", "name": "Home"}]}}}}
PRINTERS = {"data": {"printer": {"printers": {"__typename": "PrinterConnection", "edges": [{"node": {"id": "printer-1", "name": "Core", "state": "IDLE"}}]}}}}


def graphql(request):
    body = json.loads(request.content)
    if "BridgeMe" in body["query"]:
        return respx.MockResponse(json=ME)
    if "BridgePrinters" in body["query"]:
        return respx.MockResponse(json=PRINTERS)
    return respx.MockResponse(json={"data": {"printer": {"printer": printer()}}})


def make_service(tmp_path, **env):
    return Service({"DATA_DIR": str(tmp_path), "CONNECT_GRAPHQL_URL": GRAPHQL, **env})


def make_client(service):
    app = create_app(service)
    app.testing = True
    return app.test_client()


def signed_in(service):
    client = make_client(service)
    client.post("/welcome", data={"password": PASSWORD, "confirm": PASSWORD})
    return client


def configured(tmp_path, **env):
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN, CONNECT_PRINTER_UUID="printer-1", CONNECT_ORGANIZATION_ID="org-1", **env)
    service.store.save(Tokens(make_jwt(time.time() + 3600), "secret-refresh"))
    return service


def test_healthz_needs_no_login(tmp_path):
    response = make_client(make_service(tmp_path)).get("/healthz")
    assert response.status_code == 200
    assert response.json["status"] == "ok"


def test_first_visit_asks_for_a_password(tmp_path):
    client = make_client(make_service(tmp_path))
    assert "Choose an admin password" in client.get("/welcome").text
    assert client.get("/").headers["Location"].endswith("/welcome")
    assert client.get("/setup/connect").headers["Location"].endswith("/welcome")


@pytest.mark.parametrize("password,confirm", [("short", "short"), (PASSWORD, "something-else")])
def test_bad_new_password_is_refused(tmp_path, password, confirm):
    client = make_client(make_service(tmp_path))
    client.post("/welcome", data={"password": password, "confirm": confirm})
    assert not (tmp_path / "auth.json").exists() or "password_hash" not in json.loads((tmp_path / "auth.json").read_text())


def test_password_is_hashed_and_private(tmp_path):
    signed_in(make_service(tmp_path))
    path = tmp_path / "auth.json"
    assert PASSWORD not in path.read_text()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_pages_need_a_login_after_the_password_is_set(tmp_path):
    service = make_service(tmp_path)
    signed_in(service)
    stranger = make_client(service)
    for path in ("/", "/logs", "/setup/connect"):
        assert stranger.get(path).headers["Location"].endswith("/login")
    assert stranger.post("/spool/create", data={"uuid": UUID}).headers["Location"].endswith("/login")
    assert stranger.post("/welcome", data={"password": "new-password-1", "confirm": "new-password-1"}).headers["Location"].endswith("/login")


def test_login_and_logout(tmp_path, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda _: None)
    service = make_service(tmp_path)
    signed_in(service)
    client = make_client(service)
    assert "Log in" in client.get("/login").text
    client.post("/login", data={"password": "wrong-password"})
    assert client.get("/logs").status_code == 302
    client.post("/login", data={"password": PASSWORD})
    assert client.get("/logs").status_code == 200
    client.post("/logout")
    assert client.get("/logs").status_code == 302


def test_post_from_another_site_is_blocked(tmp_path):
    service = make_service(tmp_path)
    client = signed_in(service)
    response = client.post("/logout", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403


def test_wizard_starts_at_spoolman_then_connect(tmp_path):
    client = signed_in(make_service(tmp_path))
    assert client.get("/").headers["Location"].endswith("/setup/spoolman")


@respx.mock(assert_all_called=False)
def test_spoolman_test_button_does_not_save(respx_mock, tmp_path):
    respx_mock.get(f"{SPOOLMAN}/api/v1/info").respond(json={"version": "0.27.0"})
    client = signed_in(make_service(tmp_path))
    page = client.post("/setup/spoolman", data={"spoolman_url": SPOOLMAN, "action": "test"}).text
    assert "Found Spoolman 0.27.0" in page
    assert "spoolman_url" not in read_config(tmp_path)


@respx.mock(assert_all_called=False)
def test_spoolman_save_goes_to_the_next_step(respx_mock, tmp_path):
    respx_mock.get(f"{SPOOLMAN}/api/v1/info").respond(json={"version": "0.27.0"})
    service = make_service(tmp_path)
    client = signed_in(service)
    response = client.post("/setup/spoolman", data={"spoolman_url": SPOOLMAN + "/", "action": "save"})
    assert response.headers["Location"].endswith("/setup/connect")
    assert read_config(tmp_path)["spoolman_url"] == SPOOLMAN
    assert service.spoolman is not None


@respx.mock(assert_all_called=False)
def test_ca_upload_must_be_a_certificate(respx_mock, tmp_path):
    from io import BytesIO

    client = signed_in(make_service(tmp_path))
    page = client.post(
        "/setup/spoolman",
        data={"spoolman_url": "https://s.test", "action": "save", "ca_file": (BytesIO(b"not a cert"), "x.crt")},
        content_type="multipart/form-data",
    ).text
    assert "PEM format" in page
    assert not (tmp_path / "spoolman-ca.crt").exists()


@respx.mock(assert_all_called=False)
def test_connect_login_is_stored_but_never_shown(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    client = signed_in(service)
    access = make_jwt(time.time() + 3600)
    response = client.post("/setup/connect", data={"login": json.dumps({"access": access, "refresh": "secret-refresh"})})
    assert response.headers["Location"].endswith("/setup/printers")
    assert service.store.load() == Tokens(access, "secret-refresh")
    assert stat.S_IMODE(os.stat(tmp_path / "tokens.json").st_mode) == 0o600
    for path in ("/setup/connect", "/setup/printers", "/logs"):
        page = client.get(path).text
        assert access not in page and "secret-refresh" not in page
    assert "a@example.com" in client.get("/setup/connect").text


@respx.mock(assert_all_called=False)
def test_rejected_connect_login_keeps_the_old_one(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).respond(500)
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    old = Tokens(make_jwt(time.time() + 3600), "old-refresh")
    service.store.save(old)
    client = signed_in(service)
    page = client.post("/setup/connect", data={"login": json.dumps({"access": make_jwt(time.time() + 3600), "refresh": "new"})}).text
    assert "rejected the login" in page
    assert service.store.load() == old


def test_garbage_connect_login_is_refused(tmp_path):
    client = signed_in(make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN))
    assert "not recognized" in client.post("/setup/connect", data={"login": "tull"}).text
    assert not (tmp_path / "tokens.json").exists()


@respx.mock(assert_all_called=False)
def test_choosing_printers_saves_them(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    service.store.save(Tokens(make_jwt(time.time() + 3600), "r"))
    client = signed_in(service)
    assert "Core" in client.get("/setup/printers").text
    client.post("/setup/printers", data={"printer": ["printer-1"], "location_printer-1": "Workshop"})
    saved = read_config(tmp_path)["printers"][0]
    assert (saved["uuid"], saved["organization_id"], saved["location"]) == ("printer-1", "org-1", "Workshop")
    assert service.settings.enabled_printers[0].location == "Workshop"


def test_mode_step_saves_choices(tmp_path):
    service = configured(tmp_path)
    client = signed_in(service)
    client.post("/setup/mode", data={"poll_interval": "30", "auto_create": "on"})
    config = read_config(tmp_path)
    assert (config["dry_run"], config["auto_create"], config["poll_interval"]) == (False, True, 30)
    assert service.settings.dry_run is False


@respx.mock(assert_all_called=False)
def test_dashboard_shows_a_linked_spool(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[{"id": 52, "location": "Core One", "extra": {"opt_instance_uuid": json.dumps(UUID)}}])
    service = configured(tmp_path)
    assert service.poll_once() is True
    page = signed_in(service).get("/").text
    assert "PC Blend Jet Black" in page
    assert f"{SPOOLMAN}/spool/show/52" in page
    assert "#212929" in page
    assert "secret-refresh" not in page


SPOOL = {"id": 52, "remaining_weight": 1000, "initial_weight": 1000, "spool_weight": 186, "used_weight": 0, "filament": {"id": 49, "name": "Blend Jet Black", "material": "PC", "color_hex": "24292A", "weight": 1000, "spool_weight": 186, "vendor": {"name": "Prusament"}, "extra": {}}, "extra": {"card_uids": json.dumps("ABC")}}


def spoolman_writes(respx_mock):
    return [c.request for c in respx_mock.calls if c.request.method != "GET" and str(c.request.url).startswith(SPOOLMAN)]


def unlinked_service(respx_mock, tmp_path, spools=None, **env):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    mock_fields(respx_mock)
    respx_mock.get(url__startswith=f"{SPOOLMAN}/api/v1/spool?").respond(json=spools if spools is not None else [SPOOL])
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/52").respond(json=SPOOL)
    respx_mock.get(f"{SPOOLMAN}/api/v1/vendor").respond(json=[{"id": 13, "name": "Prusament"}])
    respx_mock.get(f"{SPOOLMAN}/api/v1/filament").respond(json=[])
    service = configured(tmp_path, **env)
    assert service.poll_once() is True
    return service


@respx.mock(assert_all_called=False)
def test_dashboard_preselects_the_best_match_and_writes_nothing(respx_mock, tmp_path):
    page = signed_in(unlinked_service(respx_mock, tmp_path, DRY_RUN="false")).get("/").text
    assert "Not linked yet" in page
    assert "#52 Prusament Blend Jet Black" in page
    assert "High match" in page
    assert "Same brand" in page
    assert 'value="52" checked' in page
    assert spoolman_writes(respx_mock) == []


@respx.mock(assert_all_called=False)
def test_create_new_is_preselected_when_nothing_matches(respx_mock, tmp_path):
    other = {"id": 1, "remaining_weight": 1, "filament": {"name": "Pink", "material": "PLA", "color_hex": "FF6762", "vendor": {"name": "eSun"}}, "extra": {}}
    page = signed_in(unlinked_service(respx_mock, tmp_path, spools=[other])).get("/").text
    assert 'value="new" checked' in page


@respx.mock(assert_all_called=False)
def test_review_shows_the_diff_and_writes_nothing(respx_mock, tmp_path):
    client = signed_in(unlinked_service(respx_mock, tmp_path, DRY_RUN="false"))
    page = client.get("/spool/review", query_string={"uuid": UUID, "choice": "52"}).text
    assert "Review before linking" in page
    assert "1000" in page and "959" in page and "24292A" in page and "212929" in page
    assert "card_uids" in page
    assert "other spool" not in page
    assert spoolman_writes(respx_mock) == []


@respx.mock(assert_all_called=False)
def test_review_explains_when_spoolman_has_more_usage(respx_mock, tmp_path):
    used_elsewhere = {**SPOOL, "used_weight": 120}
    client = signed_in(unlinked_service(respx_mock, tmp_path, spools=[used_elsewhere]))
    page = client.get("/spool/review", query_string={"uuid": UUID, "choice": "52"}).text
    assert "Spoolman has recorded more usage (120 g) than the tag (0 g)" in page
    assert 'value="spool_used_weight" aria-label="Apply Spool: consumed" >' in page


@respx.mock(assert_all_called=False)
def test_review_warns_about_a_shared_filament(respx_mock, tmp_path):
    other = {**SPOOL, "id": 60, "extra": {}}
    client = signed_in(unlinked_service(respx_mock, tmp_path, spools=[SPOOL, other]))
    page = client.get("/spool/review", query_string={"uuid": UUID, "choice": "52"}).text
    assert "used by 1 other spool" in page


@respx.mock(assert_all_called=False)
def test_dry_run_confirm_shows_the_calls_without_writing(respx_mock, tmp_path):
    client = signed_in(unlinked_service(respx_mock, tmp_path))
    page = client.post("/spool/link", data={"uuid": UUID, "spool_id": "52", "apply": ["spool_initial_weight"]}).text
    assert "Dry run: would perform these writes" in page
    assert "PATCH /spool/52" in page
    assert spoolman_writes(respx_mock) == []
    page = client.post("/spool/create", data={"uuid": UUID, "name": "PC Blend Jet Black", "location": "Shelf"}).text
    assert "POST /spool" in page and "POST /filament" in page and "Shelf" in page
    assert spoolman_writes(respx_mock) == []
    page = client.post("/spool/unlink", data={"spool_id": "52"}).text
    assert "PATCH /spool/52" in page
    assert spoolman_writes(respx_mock) == []


@respx.mock(assert_all_called=False)
def test_live_confirm_writes_and_links_to_spoolman(respx_mock, tmp_path):
    service = unlinked_service(respx_mock, tmp_path, DRY_RUN="false")
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/52").respond(json={})
    client = signed_in(service)
    page = client.post("/spool/link", data={"uuid": UUID, "spool_id": "52", "apply": ["spool_initial_weight"]}).text
    assert "Linked to spool 52" in page
    assert f"{SPOOLMAN}/spool/show/52" in page
    body = json.loads(patch.calls[0].request.content)
    assert json.loads(body["extra"]["opt_instance_uuid"]) == UUID
    assert body["extra"]["card_uids"] == json.dumps("ABC")
    assert body["initial_weight"] == 959
    assert "spool_weight" not in body


@respx.mock(assert_all_called=False)
def test_create_review_shows_what_is_reused(respx_mock, tmp_path):
    client = signed_in(unlinked_service(respx_mock, tmp_path))
    page = client.get("/spool/review", query_string={"uuid": UUID, "choice": "new"}).text
    assert "Uses the existing vendor" in page
    assert "Creates a new filament" in page


@respx.mock(assert_all_called=False)
def test_two_printers_are_both_polled(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[])
    (tmp_path / "config.json").write_text(json.dumps({"spoolman_url": SPOOLMAN, "printers": [
        {"uuid": "a", "organization_id": "o", "name": "A"},
        {"uuid": "b", "organization_id": "o", "name": "B", "enabled": False},
        {"uuid": "c", "organization_id": "o", "name": "C"},
    ]}))
    service = make_service(tmp_path)
    service.store.save(Tokens(make_jwt(time.time() + 3600), "r"))
    assert service.poll_once() is True
    assert set(service.status) == {"a", "c"}




def received(access, refresh, nonce):
    raw = json.dumps({"access": access, "refresh": refresh, "nonce": nonce}).encode()
    return {"login": base64.urlsafe_b64encode(raw).rstrip(b"=").decode()}


def issued_nonce(client):
    page = client.get("/setup/connect").text
    return re.search(r"nonce:&#39;([^&]+)&#39;", page).group(1)


def test_nonces_work_once_and_expire(monkeypatch):
    nonces = LoginNonces(lifetime=100)
    first = nonces.issue()
    assert nonces.consume(first) is True
    assert nonces.consume(first) is False
    assert nonces.consume("made-up") is False
    assert nonces.consume(None) is False
    second = nonces.issue()
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 101)
    assert nonces.consume(second) is False


def test_bookmarklet_only_goes_to_the_bridge_and_uses_the_fragment():
    code = bookmarklet("http://bridge:7913/setup/connect/receive", "abc")
    assert code.startswith("javascript:")
    assert "location.href='http://bridge:7913/setup/connect/receive#'+d" in code
    assert "connect.prusa3d.com" in code and "nonce:'abc'" in code
    assert "fetch" not in code


@respx.mock(assert_all_called=False)
def test_received_login_works_without_a_session_and_only_once(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    admin = signed_in(service)
    nonce = issued_nonce(admin)
    window = admin.application.test_client()
    assert "Sending the login" in window.get("/setup/connect/receive").text
    access = make_jwt(time.time() + 3600)
    response = window.post("/setup/connect/receive", json=received(access, "secret-refresh", nonce))
    assert response.status_code == 200
    assert response.json["ok"] is True
    assert "a@example.com" in response.json["message"]
    assert "secret-refresh" not in response.text
    assert service.store.load() == Tokens(access, "secret-refresh")
    again = window.post("/setup/connect/receive", json=received(make_jwt(time.time() + 3600), "other", nonce))
    assert again.status_code == 403
    assert service.store.load() == Tokens(access, "secret-refresh")


@pytest.mark.parametrize("body", [{"login": "not base64!"}, {"login": 5}, {}])
def test_bad_received_logins_are_refused(tmp_path, body):
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    signed_in(service)
    assert make_client(service).post("/setup/connect/receive", json=body).status_code == 403
    assert not service.store.exists()


def test_received_login_with_an_unknown_nonce_is_refused(tmp_path):
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    signed_in(service)
    response = make_client(service).post("/setup/connect/receive", json=received(make_jwt(time.time() + 3600), "r", "made-up"))
    assert response.status_code == 403
    assert not service.store.exists()


@respx.mock(assert_all_called=False)
def test_received_login_with_no_token_is_refused(respx_mock, tmp_path):
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    admin = signed_in(service)
    nonce = issued_nonce(admin)
    assert admin.application.test_client().post("/setup/connect/receive", json=received(None, None, nonce)).status_code == 400
    assert not service.store.exists()


def test_received_login_from_another_site_is_blocked(tmp_path):
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    client = signed_in(service)
    nonce = issued_nonce(client)
    response = client.post("/setup/connect/receive", json=received("a", "r", nonce), headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


@respx.mock(assert_all_called=False)
def test_recent_actions_show_their_text(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    mock_fields(respx_mock)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool").respond(json=[{"id": 52, "location": "Shelf", "extra": {"opt_instance_uuid": json.dumps(UUID)}}])
    respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/52").respond(json={})
    service = configured(tmp_path, DRY_RUN="false", LOCATION_NAME="Core One")
    assert service.poll_once() is True
    page = signed_in(service).get("/").text
    assert "PATCH /spool/52" in page
    assert "Called" in page


def custom_graphql(request):
    body = json.loads(request.content)
    if "BridgePrinter(" not in body["query"]:
        return graphql(request)
    data = printer(uuid="custom", from_nfc=False)
    instance = data["fff"]["tools"][0]["materialPackageInstance"]
    instance["actualNettoFullWeight"] = None
    instance["package"]["container"] = None
    instance["package"]["material"].update(name=None, brand=None, properties={"density": None}, type={"abbreviation": "PETG"})
    return respx.MockResponse(json={"data": {"printer": {"printer": data}}})


@respx.mock(assert_all_called=False)
def test_a_custom_tag_spool_can_be_linked_but_not_created(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).mock(side_effect=custom_graphql)
    mock_fields(respx_mock)
    petg = {"id": 55, "remaining_weight": 1000, "initial_weight": 1000, "used_weight": 0, "filament": {"id": 5, "name": "petg-gf", "material": "PETG", "color_hex": "000000", "weight": 1000, "vendor": {"name": "ELEGOO"}, "extra": {}}, "extra": {"card_uids": json.dumps("")}}
    respx_mock.get(url__startswith=f"{SPOOLMAN}/api/v1/spool?").respond(json=[petg])
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/55").respond(json=petg)
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/55").respond(json={})
    service = configured(tmp_path, DRY_RUN="false")
    assert service.poll_once() is True
    client = signed_in(service)
    page = client.get("/").text
    assert "Unknown brand PETG" in page
    assert "1000 g nominal" in page
    assert "#55 ELEGOO petg-gf" in page
    assert "Not confirmed by Connect" in page
    assert 'value="new"' not in page
    assert 'value="55" checked' not in page
    assert client.get("/spool/review", query_string={"uuid": "custom", "choice": "new"}).status_code == 302
    review = client.get("/spool/review", query_string={"uuid": "custom", "choice": "55"}).text
    assert "Review before linking" in review
    assert "Link the Unknown brand PETG" in review
    assert "Linked to spool 55" in client.post("/spool/link", data={"uuid": "custom", "spool_id": "55"}).text
    body = json.loads(patch.calls[0].request.content)
    assert set(body) == {"extra"}
    assert json.loads(body["extra"]["opt_instance_uuid"]) == "custom"


@respx.mock(assert_all_called=False)
def test_another_spool_list_is_sorted_by_id_and_searchable(respx_mock, tmp_path):
    spools = [
        {"id": 7, "remaining_weight": 1, "filament": {"name": "Pink", "material": "PLA", "color_hex": "ff6762", "vendor": {"name": "eSun"}}, "extra": {}},
        {"id": 52, "remaining_weight": 2, "filament": {"name": "Black", "material": "PC", "vendor": {"name": "Prusament"}}, "extra": {}},
        {"id": 3, "remaining_weight": 3, "filament": {"name": "Blue", "material": "PETG", "color_hex": "0000FF", "vendor": {"name": "Polymaker"}}, "extra": {}},
    ]
    page = signed_in(unlinked_service(respx_mock, tmp_path, spools=spools)).get("/").text
    listed = page[page.index('name="other_spool"'):page.index("</select>")]
    assert re.findall(r'<option value="(\d+)"', listed) == ["3", "7", "52"]
    assert 'data-search="#7 eSun Pink (PLA, 1 g) #FF6762"' in listed
    assert "data-spool-filter" in page


# The Connect warehouse

WAREHOUSE_UUID = "9f1c2d3e-0000-5000-8000-000000000001"
# The shape from research, with made-up identifiers.
WAREHOUSE_ITEMS = [
    {
        "name": "PETG-GF Black",
        "consumedWeight": 6,
        "nominalNettoFullWeight": 1000,
        "empty": False,
        "archived": False,
        "location": "Shelf A",
        "materialPackageInstanceUuidSource": "generated",
        "materialPackageInstance": {
            "uuid": WAREHOUSE_UUID,
            "actualNettoFullWeight": None,
            "package": {
                "filamentDiameter": 1.75,
                "container": {"emptyWeight": 220},
                "material": {
                    "uuid": "9f1c2d3e-0000-5000-8000-000000000002",
                    "type": "PETG",
                    "primaryColor": {"colorRgba": "#101010"},
                    "properties": {"density": 1.35},
                    "brand": {"name": "ELEGOO"},
                },
            },
        },
    }
]


def warehouse_service(respx_mock, tmp_path, items=None, spools=None, printer_data=None, **env):
    reads = []

    def answer(request):
        body = json.loads(request.content)
        if "BridgeWarehouse" in body["query"]:
            reads.append(body["variables"]["organizationId"])
            if isinstance(items, Exception):
                return respx.MockResponse(500)
            page = {"pageInfo": {"hasNextPage": False}, "nodes": WAREHOUSE_ITEMS if items is None else items}
            return respx.MockResponse(json={"data": {"warehouse": {"items": page}}})
        data = printer_data or {**printer(), "fff": {"tools": []}}
        return respx.MockResponse(json={"data": {"printer": {"printer": data}}})

    respx_mock.post(GRAPHQL).mock(side_effect=answer)
    mock_fields(respx_mock)
    all_spools = [SPOOL] if spools is None else spools
    respx_mock.get(url__startswith=f"{SPOOLMAN}/api/v1/spool?").respond(json=all_spools)
    respx_mock.get(f"{SPOOLMAN}/api/v1/spool/52").respond(json=SPOOL)
    respx_mock.get(f"{SPOOLMAN}/api/v1/vendor").respond(json=[])
    respx_mock.get(f"{SPOOLMAN}/api/v1/filament").respond(json=[])
    service = configured(tmp_path, **env)
    assert service.poll_once() is True
    service.warehouse_reads = reads
    return service


@respx.mock(assert_all_called=False)
def test_dashboard_lists_an_unlinked_warehouse_spool_with_a_link_form(respx_mock, tmp_path):
    page = signed_in(warehouse_service(respx_mock, tmp_path, DRY_RUN="false")).get("/").text
    section = page[page.index("<h2>Warehouse</h2>"):]
    assert "1 not linked" in section and "0 linked" in section
    assert "ELEGOO PETG-GF Black" in section and "PETG" in section and "#101010" in section and "1000 g nominal" in section
    assert f'name="uuid" value="{WAREHOUSE_UUID}"' in section
    assert "#52 Prusament Blend Jet Black" in section
    assert 'value="new"' in section
    assert "data-spool-filter" in section
    assert spoolman_writes(respx_mock) == []


@respx.mock(assert_all_called=False)
def test_a_linked_warehouse_spool_is_only_counted(respx_mock, tmp_path):
    linked = {**SPOOL, "extra": {"opt_instance_uuid": json.dumps(WAREHOUSE_UUID)}}
    page = signed_in(warehouse_service(respx_mock, tmp_path, spools=[linked])).get("/").text
    section = page[page.index("<h2>Warehouse</h2>"):]
    assert "0 not linked" in section and "1 linked" in section
    assert "ELEGOO PETG-GF Black" not in section
    assert 'http-equiv="refresh"' in page


@respx.mock(assert_all_called=False)
def test_a_warehouse_spool_in_a_printer_points_to_the_printer(respx_mock, tmp_path):
    loaded = printer(uuid=WAREHOUSE_UUID)
    service = warehouse_service(respx_mock, tmp_path, spools=[SPOOL], printer_data=loaded)
    page = signed_in(service).get("/").text
    section = page[page.index("<h2>Warehouse</h2>"):]
    assert "Loaded in Core" in section
    assert f'name="uuid" value="{WAREHOUSE_UUID}"' not in section


@respx.mock(assert_all_called=False)
def test_auto_create_does_not_create_a_spool_that_is_not_loaded(respx_mock, tmp_path):
    warehouse_service(respx_mock, tmp_path, DRY_RUN="false", AUTO_CREATE="true")
    assert spoolman_writes(respx_mock) == []


@respx.mock(assert_all_called=False)
def test_linking_a_warehouse_spool_writes_only_after_confirming(respx_mock, tmp_path):
    patch = respx_mock.patch(f"{SPOOLMAN}/api/v1/spool/52").respond(json={})
    filament = respx_mock.patch(f"{SPOOLMAN}/api/v1/filament/49").respond(json={})
    client = signed_in(warehouse_service(respx_mock, tmp_path, DRY_RUN="false"))
    review = client.get("/spool/review", query_string={"uuid": WAREHOUSE_UUID, "choice": "52"}).text
    assert "Review before linking" in review
    assert "in tool" not in review
    assert 'value="filament_material" aria-label="Apply Filament: OpenPrintTag material" checked' in review
    assert spoolman_writes(respx_mock) == []
    client.post("/spool/link", data={"uuid": WAREHOUSE_UUID, "spool_id": "52", "apply": ["filament_material"]})
    assert extra_value(json.loads(patch.calls[0].request.content), "opt_instance_uuid") == WAREHOUSE_UUID
    assert extra_value(json.loads(filament.calls[0].request.content), "opt_material_uuid") == "9f1c2d3e-0000-5000-8000-000000000002"


@respx.mock(assert_all_called=False)
def test_a_dry_run_shows_the_calls_for_a_warehouse_spool_without_making_them(respx_mock, tmp_path):
    client = signed_in(warehouse_service(respx_mock, tmp_path))
    linked = client.post("/spool/link", data={"uuid": WAREHOUSE_UUID, "spool_id": "52"}).text
    assert "PATCH /spool/52" in linked
    created = client.post("/spool/create", data={"uuid": WAREHOUSE_UUID, "name": "PETG-GF Black", "location": ""}).text
    assert "POST /spool" in created and "POST /filament" in created
    assert spoolman_writes(respx_mock) == []


@respx.mock(assert_all_called=False)
def test_the_create_review_for_a_warehouse_spool_uses_its_location(respx_mock, tmp_path):
    client = signed_in(warehouse_service(respx_mock, tmp_path))
    page = client.get("/spool/review", query_string={"uuid": WAREHOUSE_UUID, "choice": "new"}).text
    assert "in tool" not in page
    assert 'value="Shelf A"' in page
    assert "1000 g filament" in page


@respx.mock(assert_all_called=False)
def test_the_warehouse_is_read_at_its_own_interval(respx_mock, tmp_path):
    service = warehouse_service(respx_mock, tmp_path)
    assert service.warehouse_reads == ["org-1"]
    assert service.poll_once() is True
    assert service.warehouse_reads == ["org-1"]
    client = signed_in(service)
    client.post("/poll")
    assert service.poll_once() is True
    assert service.warehouse_reads == ["org-1", "org-1"]


@respx.mock(assert_all_called=False)
def test_a_warehouse_failure_does_not_stop_the_poll(respx_mock, tmp_path):
    service = warehouse_service(respx_mock, tmp_path, items=RuntimeError("down"))
    page = signed_in(service).get("/").text
    assert "Could not read the Connect warehouse" in page


def test_mode_step_saves_track_usage(tmp_path):
    service = make_service(tmp_path)
    client = signed_in(service)
    client.post("/setup/mode", data={"poll_interval": "60"})
    assert read_config(tmp_path)["track_usage"] is False
    assert service.settings.track_usage is False
    client.post("/setup/mode", data={"poll_interval": "60", "track_usage": "on"})
    assert read_config(tmp_path)["track_usage"] is True
    assert service.settings.track_usage is True


def test_the_warehouse_interval_can_be_saved(tmp_path):
    service = make_service(tmp_path)
    client = signed_in(service)
    client.post("/setup/mode", data={"poll_interval": "60", "warehouse_interval": "900"})
    assert read_config(tmp_path)["warehouse_interval"] == 900
    assert service.settings.warehouse_interval == 900


# The warehouse page

def item(uuid, name, material="PETG", consumed=0, location="", empty=False, color="#101010"):
    found = json.loads(json.dumps(WAREHOUSE_ITEMS[0]))
    found.update(name=name, consumedWeight=consumed, location=location, empty=empty)
    found["materialPackageInstance"]["uuid"] = uuid
    found["materialPackageInstance"]["package"]["material"].update(type=material, primaryColor={"colorRgba": color})
    return found


@respx.mock(assert_all_called=False)
def test_warehouse_page_lists_every_spool_with_its_link(respx_mock, tmp_path):
    other = "9f1c2d3e-0000-5000-8000-000000000003"
    items = [
        item(WAREHOUSE_UUID, "PETG-GF Black", consumed=6, location="Shelf A"),
        item(other, "PLA Galaxy", material="PLA", consumed=750, color="#5A2D82"),
        item("9f1c2d3e-0000-5000-8000-000000000004", "Old ASA", material="ASA", consumed=1000, empty=True),
    ]
    linked = {**SPOOL, "remaining_weight": 990, "extra": {"opt_instance_uuid": json.dumps(WAREHOUSE_UUID)}}
    client = signed_in(warehouse_service(respx_mock, tmp_path, items=items, spools=[linked]))
    page = client.get("/warehouse").text
    assert 'href="/warehouse" aria-current=page' in page
    assert "ELEGOO – PETG-GF Black" in page and "Shelf A" in page and "994 g" in page
    assert 'title="Open spool 52 in Spoolman">52</a>' in page and "990 g" in page
    assert "ELEGOO – PLA Galaxy" in page and "250 g" in page
    assert 'data-link-toggle="link-9f1c2d3e-0000-5000-8000-000000000003"' in page
    assert f'name="uuid" value="{other}"' in page
    assert f'name="uuid" value="{WAREHOUSE_UUID}"' not in page
    assert "Empty spools (1)" in page and "ELEGOO – Old ASA" in page
    # Two spools left: 994 g + 250 g. The empty one is not counted.
    assert "1.2 kg" in page
    assert re.search(r'Not linked</span>\s*<strong>1</strong>', page)
    assert spoolman_writes(respx_mock) == []


@respx.mock(assert_all_called=False)
def test_warehouse_page_shows_a_loaded_spool_in_its_printer(respx_mock, tmp_path):
    service = warehouse_service(respx_mock, tmp_path, spools=[SPOOL], printer_data=printer(uuid=WAREHOUSE_UUID))
    page = signed_in(service).get("/warehouse").text
    assert '<span class="pill state-printing">Core</span>' in page
    assert 'title="Link it on the dashboard"' in page
    assert f'name="uuid" value="{WAREHOUSE_UUID}"' not in page


@respx.mock(assert_all_called=False)
def test_warehouse_page_lists_a_loaded_spool_that_is_not_in_the_warehouse(respx_mock, tmp_path):
    service = warehouse_service(respx_mock, tmp_path, items=[], spools=[SPOOL], printer_data=printer())
    page = signed_in(service).get("/warehouse").text
    assert "Not in Warehouse" in page and '<span class="pill state-printing">Core</span>' in page


@respx.mock(assert_all_called=False)
def test_warehouse_page_still_works_without_spoolman(respx_mock, tmp_path):
    service = warehouse_service(respx_mock, tmp_path)
    respx_mock.get(url__startswith=f"{SPOOLMAN}/api/v1/spool?").respond(500)
    page = signed_in(service).get("/warehouse").text
    assert "links are not shown" in page
    assert "ELEGOO – PETG-GF Black" in page
    assert 'name="uuid"' not in page


@respx.mock(assert_all_called=False)
def test_warehouse_page_reports_a_failed_read(respx_mock, tmp_path):
    page = signed_in(warehouse_service(respx_mock, tmp_path, items=RuntimeError())).get("/warehouse").text
    assert "Could not read the Connect warehouse" in page


@respx.mock(assert_all_called=False)
def test_warehouse_read_now_asks_for_a_new_read(respx_mock, tmp_path):
    service = warehouse_service(respx_mock, tmp_path)
    client = signed_in(service)
    service.warehouse_due = time.time() + 999
    response = client.post("/warehouse/refresh")
    assert response.status_code == 302 and response.headers["Location"].endswith("/warehouse")
    assert service.warehouse_due == 0.0
