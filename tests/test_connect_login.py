import time

import respx

from bridge.web import LoginNonces
from tests.test_bridge import GRAPHQL, SPOOLMAN, make_jwt
from tests.test_web import graphql, issued_nonce, make_client, make_service, received, signed_in


def test_a_nonce_is_waiting_then_delivered():
    nonces = LoginNonces(lifetime=100)
    nonce = nonces.issue()
    assert nonces.status(nonce) == ("waiting", None)
    assert nonces.consume(nonce)
    nonces.finish(nonce, "delivered", "Logged in as a@example.com.")
    assert nonces.status(nonce) == ("delivered", "Logged in as a@example.com.")
    assert nonces.status("made-up") == ("expired", None)
    assert nonces.status(None) == ("expired", None)


def test_an_unused_nonce_expires(monkeypatch):
    nonces = LoginNonces(lifetime=100)
    nonce = nonces.issue()
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 101)
    assert nonces.status(nonce) == ("expired", None)


@respx.mock(assert_all_called=False)
def test_the_setup_page_learns_that_the_bookmark_delivered_the_login(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    admin = signed_in(service)
    nonce = issued_nonce(admin)
    assert admin.get(f"/setup/connect/status?nonce={nonce}").json == {"state": "waiting", "message": None}
    window = admin.application.test_client()
    assert window.post("/setup/connect/receive", json=received(make_jwt(time.time() + 3600), "secret-refresh", nonce)).json["ok"]
    status = admin.get(f"/setup/connect/status?nonce={nonce}")
    assert status.json == {"state": "delivered", "message": "Logged in as a@example.com."}
    assert "secret-refresh" not in status.text


@respx.mock(assert_all_called=False)
def test_a_rejected_login_is_reported_with_its_reason(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).respond(500)
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    admin = signed_in(service)
    nonce = issued_nonce(admin)
    admin.application.test_client().post("/setup/connect/receive", json=received(make_jwt(time.time() + 3600), "r", nonce))
    status = admin.get(f"/setup/connect/status?nonce={nonce}").json
    assert status["state"] == "failed"
    assert status["message"].startswith("Connect rejected the login")


@respx.mock(assert_all_called=False)
def test_a_login_without_refresh_token_is_a_warning(respx_mock, tmp_path):
    respx_mock.post(GRAPHQL).mock(side_effect=graphql)
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    admin = signed_in(service)
    nonce = issued_nonce(admin)
    admin.application.test_client().post("/setup/connect/receive", json=received(make_jwt(time.time() + 3600), None, nonce))
    status = admin.get(f"/setup/connect/status?nonce={nonce}").json
    assert status["state"] == "warning"
    assert "no refresh token" in status["message"]


def test_the_status_needs_the_admin_session(tmp_path):
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    admin = signed_in(service)
    nonce = issued_nonce(admin)
    assert make_client(service).get(f"/setup/connect/status?nonce={nonce}").headers["Location"].endswith("/login")


def test_the_setup_page_waits_and_offers_the_code_for_phones(tmp_path):
    page = signed_in(make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)).get("/setup/connect").text
    assert "data-login-wait=" in page
    assert 'data-copy="bookmarklet-code"' in page
    assert 'id="bookmarklet-code">javascript:' in page
    assert "reload this page." not in page
    assert "data-logged-in" not in page


def test_the_wait_stays_quiet_when_already_logged_in(tmp_path):
    from bridge.connect import Tokens
    service = make_service(tmp_path, SPOOLMAN_URL=SPOOLMAN)
    service.store.save(Tokens(make_jwt(time.time() + 3600), "secret-refresh"))
    page = signed_in(service).get("/setup/connect").text
    assert "data-logged-in" in page
