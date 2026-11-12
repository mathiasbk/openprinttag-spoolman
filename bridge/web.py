"""The web interface: a setup wizard and a dashboard, behind one admin password."""

import base64
import json
import logging
import os
import secrets
import threading
import time
from dataclasses import replace
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .config import update_config
from .connect import clean_token, parse_login_json
from .service import Service
from .spoolman import SpoolmanClient

log = logging.getLogger(__name__)

STEPS = ("spoolman", "connect", "printers", "mode")
MIN_PASSWORD_LENGTH = 8
CA_FILENAME = "spoolman-ca.crt"
NONCE_LIFETIME_SECONDS = 15 * 60
# Connect counts whole grams, and the two can be a few grams apart while a print runs.
WEIGHT_MISMATCH_GRAMS = 10
CONSOLE_LINE = (
    "copy(JSON.stringify({access: localStorage.getItem('auth.access_token'), "
    "refresh: localStorage.getItem('auth.refresh_token')}))"
)


class LoginNonces:
    """One-time codes that let the bookmarklet's window deliver a login without an admin session."""

    def __init__(self, lifetime: int = NONCE_LIFETIME_SECONDS):
        self.lifetime = lifetime
        self.lock = threading.Lock()
        self.expiries: dict[str, float] = {}
        self.results: dict[str, tuple[float, str, str]] = {}

    def issue(self) -> str:
        nonce = secrets.token_urlsafe(24)
        now = time.time()
        with self.lock:
            self.expiries = {n: e for n, e in self.expiries.items() if e > now}
            self.expiries[nonce] = now + self.lifetime
        return nonce

    def consume(self, nonce) -> bool:
        with self.lock:
            expiry = self.expiries.pop(nonce, 0) if isinstance(nonce, str) else 0
        return expiry > time.time()

    def finish(self, nonce: str, state: str, message: str) -> None:
        """Remember what a used code brought, so the setup page can tell: delivered, warning or failed."""
        with self.lock:
            now = time.time()
            self.results = {n: r for n, r in self.results.items() if r[0] > now}
            self.results[nonce] = (now + self.lifetime, state, message)

    def status(self, nonce) -> tuple[str, str | None]:
        """(state, message): delivered, warning or failed once used, otherwise waiting or expired."""
        with self.lock:
            if isinstance(nonce, str) and nonce in self.results:
                _, state, message = self.results[nonce]
                return state, message
            return ("waiting" if self.expiries.get(nonce, 0) > time.time() else "expired"), None


def bookmarklet(receive_url: str, nonce: str) -> str:
    """Reads the login on connect.prusa3d.com and navigates to the bridge with it in the fragment.

    It navigates instead of using fetch, because an HTTPS page may not call a plain HTTP bridge.
    """
    return (
        "javascript:(function(){"
        "var a=localStorage.getItem('auth.access_token'),r=localStorage.getItem('auth.refresh_token');"
        "if(location.hostname!=='connect.prusa3d.com'||!a){alert('Log in on connect.prusa3d.com first, then click this bookmark there.');return;}"
        f"var d=btoa(JSON.stringify({{access:a,refresh:r,nonce:'{nonce}'}}))"
        ".replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');"
        f"location.href='{receive_url}#'+d;"
        "})();"
    )


def decode_received_login(raw) -> dict | None:
    try:
        padded = raw + "=" * (-len(raw) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded))
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


class AuthStore:
    """The password hash and the session signing key, in a private file."""

    def __init__(self, path: Path):
        self.path = path

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, self.path)

    def secret_key(self) -> str:
        data = self._read()
        if "secret_key" not in data:
            data["secret_key"] = secrets.token_hex(32)
            self._write(data)
        return data["secret_key"]

    def has_password(self) -> bool:
        return "password_hash" in self._read()

    def set_password(self, password: str) -> None:
        data = self._read()
        data["password_hash"] = generate_password_hash(password)
        self._write(data)

    def check_password(self, password: str) -> bool:
        stored = self._read().get("password_hash")
        return bool(stored) and check_password_hash(stored, password)


def next_step(service: Service) -> str | None:
    """The first wizard step that still needs doing."""
    if not service.settings.spoolman_url or not service.spoolman:
        return "spoolman"
    if not service.store.exists():
        return "connect"
    if not service.settings.printers:
        return "printers"
    return None


def create_app(service: Service) -> Flask:
    app = Flask(__name__)
    auth = AuthStore(service.settings.data_dir / "auth.json")
    nonces = LoginNonces()
    app.secret_key = auth.secret_key()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Strict",
        MAX_CONTENT_LENGTH=1024 * 1024,
    )

    @app.template_filter("clock")
    def clock(timestamp):
        return datetime.fromtimestamp(timestamp).strftime("%H:%M:%S") if timestamp else ""

    @app.template_filter("grams")
    def grams(value):
        return "unknown" if value is None else f"{round(value):d} g"

    @app.template_filter("kilograms")
    def kilograms(value):
        return f"{round(value):d} g" if value < 1000 else f"{value / 1000:.1f} kg"

    @app.template_filter("until")
    def until(timestamp):
        if not timestamp:
            return "unknown"
        seconds = timestamp - time.time()
        if seconds <= 0:
            return "expired"
        return f"{int(seconds // 60)} min"

    @app.context_processor
    def globals_for_templates():
        return {"settings": service.settings, "signed_in": session.get("user") == "admin"}

    @app.before_request
    def guard():
        # A form posted from another site must not act on the admin's session.
        if request.method == "POST":
            origin = request.headers.get("Origin")
            if origin and urlparse(origin).netloc != request.host:
                return "Blocked: the request came from another site", 403
        if request.endpoint in ("healthz", "static", "login", "first_password", "receive_login", "receive_login_post"):
            return None
        if not auth.has_password():
            return redirect(url_for("first_password"))
        if session.get("user") != "admin":
            return redirect(url_for("login"))
        return None

    @app.after_request
    def security_headers(response):
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; frame-ancestors 'none'"
        return response

    @app.get("/setup/connect/receive")
    def receive_login():
        return render_template("receive.html")

    @app.post("/setup/connect/receive")
    def receive_login_post():
        """The private window has no admin session, so the one-time code is what authorizes this."""
        data = decode_received_login((request.get_json(silent=True) or {}).get("login"))
        if not data or not nonces.consume(data.get("nonce")):
            return jsonify(ok=False, message="This login link has expired or was already used. Reload the setup page and drag the new link to your bookmarks."), 403
        parsed = parse_login_json(json.dumps({"access": data.get("access"), "refresh": data.get("refresh")}))
        if not parsed:
            message = "No Prusa Connect login was found. Log in on connect.prusa3d.com first."
            nonces.finish(data["nonce"], "failed", message)
            return jsonify(ok=False, message=message), 400
        try:
            me = service.log_in_to_connect(*parsed)
        except Exception as e:
            log.warning("A login from the bookmark was not accepted: %s", e)
            message = f"Connect rejected the login: {str(e).rstrip('.')}."
            nonces.finish(data["nonce"], "failed", message)
            return jsonify(ok=False, message=message), 400
        if not parsed[1]:
            message = f"Logged in as {me.get('email')}, but there is no refresh token, so the bridge stops when the login expires."
            nonces.finish(data["nonce"], "warning", message)
        else:
            message = f"Logged in as {me.get('email')}."
            nonces.finish(data["nonce"], "delivered", message)
        return jsonify(ok=True, message=message)

    @app.get("/setup/connect/status")
    def login_status():
        """The setup page asks this while it waits for the bookmark, so it can continue by itself."""
        state, message = nonces.status(request.args.get("nonce"))
        return jsonify(state=state, message=message)

    @app.get("/healthz")
    def healthz():
        alive = service.poller_alive()
        return jsonify(status="ok" if alive else "poller stopped"), 200 if alive else 503

    @app.route("/welcome", methods=["GET", "POST"])
    def first_password():
        if auth.has_password():
            return redirect(url_for("login"))
        if request.method == "POST":
            password = request.form.get("password", "")
            if len(password) < MIN_PASSWORD_LENGTH:
                flash(f"The password must have at least {MIN_PASSWORD_LENGTH} characters.", "error")
            elif password != request.form.get("confirm"):
                flash("The passwords do not match.", "error")
            else:
                auth.set_password(password)
                session["user"] = "admin"
                session.permanent = True
                return redirect(url_for("dashboard"))
        return render_template("password.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not auth.has_password():
            return redirect(url_for("first_password"))
        if request.method == "POST":
            if auth.check_password(request.form.get("password", "")):
                session["user"] = "admin"
                session.permanent = True
                return redirect(url_for("dashboard"))
            time.sleep(1)
            flash("Wrong password.", "error")
        return render_template("login.html")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def dashboard():
        step = next_step(service)
        if step:
            return redirect(url_for("setup", step=step))
        printers = []
        problems = []
        warehouse = None
        with service.lock:
            try:
                spools = service.list_spools()
            except Exception as e:
                problems.append(f"Could not read spools from Spoolman: {e}")
                spools = None
            for config in service.settings.enabled_printers:
                entry = dict(service.status.get(config.uuid) or {})
                entry["config"] = config
                tools = []
                for tool in entry.get("tools", []):
                    view = dict(tool)
                    if tool["spool_id"]:
                        view["url"] = service.spoolman.spool_url(tool["spool_id"])
                        linked = next((s for s in spools or [] if s["id"] == tool["spool_id"]), None)
                        view["spoolman_remaining"] = linked.get("remaining_weight") if linked else None
                        connect_remaining = tool["spool"].remaining_weight
                        if view["spoolman_remaining"] is not None and connect_remaining is not None:
                            difference = view["spoolman_remaining"] - connect_remaining
                            view["weight_difference"] = difference if abs(difference) >= WEIGHT_MISMATCH_GRAMS else None
                            # In a dry run Spoolman lags behind Connect by what has not been deducted yet.
                            view["not_deducted_yet"] = service.settings.dry_run and difference > 0
                    else:
                        view["candidates"] = service.link_candidates(tool["spool"], spools or [])
                    tools.append(view)
                entry["tools"] = tools
                printers.append(entry)
            if spools is not None:
                warehouse = service.warehouse_view(spools)
                for entry in warehouse["unlinked"]:
                    entry["candidates"] = service.link_candidates(entry["spool"], spools)
        needs_choice = any("candidates" in tool for printer in printers for tool in printer["tools"]) or bool(warehouse and warehouse["unlinked"])
        return render_template("dashboard.html", printers=printers, problems=problems, token=service.token_status(), needs_choice=needs_choice, warehouse=warehouse)

    @app.get("/warehouse")
    def warehouse():
        step = next_step(service)
        if step:
            return redirect(url_for("setup", step=step))
        problems = []
        with service.lock:
            try:
                spools = service.list_spools()
            except Exception as e:
                problems.append(f"Could not read spools from Spoolman, so links are not shown: {e}")
                spools = None
            overview = service.warehouse_overview(spools)
            for row in overview["rows"]:
                if spools is not None and not row["linked"] and not row["printer"] and not row["empty"]:
                    row["candidates"] = service.link_candidates(row["spool"], spools)
        return render_template("warehouse.html", overview=overview, problems=problems)

    @app.post("/warehouse/refresh")
    def refresh_warehouse():
        service.refresh_warehouse_soon()
        flash("Reading the Connect warehouse now. Reload this page in a moment.", "ok")
        return redirect(url_for("warehouse"))

    @app.get("/logs")
    def logs():
        return render_template("logs.html", lines=list(service.logs.lines)[::-1])

    @app.post("/poll")
    def poll_now():
        service.refresh_warehouse_soon()
        flash("Polling the printers now. Reload this page in a moment.", "ok")
        return redirect(url_for("dashboard"))

    def result(heading, calls, spool_id=None):
        url = service.spoolman.spool_url(spool_id) if spool_id and not service.settings.dry_run else None
        return render_template("result.html", heading=heading, calls=calls, spool_url=url)

    def failed(prefix, error):
        flash(f"{prefix}: {error}", "error")
        return redirect(url_for("dashboard"))

    @app.get("/spool/review")
    def review_choice():
        """Reads only. The dashboard's choice opens the matching review page."""
        uuid = request.args.get("uuid", "")
        choice = request.args.get("choice", "")
        if choice == "other":
            choice = request.args.get("other_spool", "")
        try:
            if choice == "new":
                return render_template("create_review.html", **service.create_review(uuid))
            return render_template("link_review.html", **service.link_review(uuid, int(choice)))
        except ValueError as e:
            return failed("Could not open the review", e if choice == "new" else "choose a spool that is still available")
        except Exception as e:
            return failed("Could not open the review", e)

    @app.post("/spool/link")
    def link_spool():
        try:
            spool_id = int(request.form["spool_id"])
            calls = service.link(request.form["uuid"], spool_id, set(request.form.getlist("apply")))
        except (KeyError, ValueError):
            return failed("Could not link the spool", "that spool is no longer available")
        except Exception as e:
            return failed("Could not link the spool", e)
        heading = "Nothing was linked" if service.settings.dry_run else f"Linked to spool {spool_id}"
        return result(heading, calls, spool_id)

    @app.post("/spool/create")
    def create_spool():
        try:
            calls = service.create(request.form["uuid"], request.form.get("name", "").strip()[:64], request.form.get("location", "").strip()[:64])
        except Exception as e:
            return failed("Could not create the spool", e)
        return result("Nothing was created" if service.settings.dry_run else "Created the spool", calls)

    @app.get("/spool/<int:spool_id>/unlink")
    def unlink_review(spool_id):
        return render_template("unlink.html", spool_id=spool_id)

    @app.post("/spool/unlink")
    def unlink_spool():
        try:
            spool_id = int(request.form["spool_id"])
            calls = service.unlink(spool_id)
        except Exception as e:
            return failed("Could not unlink the spool", e)
        return result("Nothing was unlinked" if service.settings.dry_run else f"Unlinked spool {spool_id}", calls, spool_id)

    def advance(step: str):
        service.reload()
        index = STEPS.index(step)
        return redirect(url_for("setup", step=STEPS[min(index + 1, len(STEPS) - 1)]))

    @app.route("/setup/<step>", methods=["GET", "POST"])
    def setup(step):
        if step not in STEPS:
            return redirect(url_for("setup", step=STEPS[0]))
        context = {}
        if request.method == "POST":
            response = {
                "spoolman": _post_spoolman,
                "connect": _post_connect,
                "printers": _post_printers,
                "mode": _post_mode,
            }[step]()
            if response is not None:
                return response
            if request.form.get("action") == "test":
                context["test"] = request.environ.get("bridge.test_result")
        if step == "printers":
            context["known"] = {p.uuid: p for p in service.settings.printers}
            try:
                with service.lock:
                    context["available"] = service.connect.list_printers()
            except Exception as e:
                context["available"] = []
                flash(f"Could not read printers from Connect: {e}", "error")
        if step == "connect":
            context["token"] = service.token_status()
            context["console_line"] = CONSOLE_LINE
            context["nonce"] = nonces.issue()
            context["bookmarklet"] = bookmarklet(url_for("receive_login", _external=True), context["nonce"])
        return render_template("setup.html", step=step, steps=STEPS, form=request.form, **context)

    def _post_spoolman():
        url = request.form.get("spoolman_url", "").strip().rstrip("/")
        verify = request.form.get("verify_tls", "on") == "on"
        if urlparse(url).scheme not in ("http", "https") or not urlparse(url).netloc:
            flash("Enter the address as http://server:7912 or https://spoolman.example.", "error")
            return None
        ca_path = service.settings.spoolman_ca_bundle
        upload = request.files.get("ca_file")
        if upload and upload.filename:
            content = upload.read()
            if b"BEGIN CERTIFICATE" not in content:
                flash("That file does not look like a certificate in PEM format.", "error")
                return None
            target = service.settings.data_dir / CA_FILENAME
            target.write_bytes(content)
            ca_path = str(target)
        try:
            client = SpoolmanClient(replace(service.settings, spoolman_url=url, spoolman_ca_bundle=ca_path, spoolman_verify_tls=verify))
            version = client.info().get("version")
        except Exception as e:
            flash(f"Could not reach Spoolman: {e}", "error")
            return None
        if request.form.get("action") == "test":
            request.environ["bridge.test_result"] = f"Found Spoolman {version}."
            return None
        update_config(service.settings.data_dir, {"spoolman_url": url, "spoolman_ca_bundle": ca_path, "spoolman_verify_tls": verify})
        flash(f"Saved. Found Spoolman {version}.", "ok")
        return advance("spoolman")

    def _post_connect():
        raw = request.form.get("login", "").strip()
        parsed = parse_login_json(raw)
        if not parsed:
            flash("That was not recognized. Run the line in the console again and paste the whole result.", "error")
            return None
        access, refresh = clean_token(parsed[0]), parsed[1]
        try:
            me = service.log_in_to_connect(access, refresh)
        except Exception as e:
            flash(f"Connect rejected the login: {e}", "error")
            return None
        if refresh:
            flash(f"Logged in as {me.get('email')}.", "ok")
        else:
            flash("The login works, but there is no refresh token, so the bridge stops when it expires.", "error")
        return advance("connect")

    def _post_printers():
        printers = []
        try:
            with service.lock:
                available = service.connect.list_printers()
        except Exception as e:
            flash(f"Could not read printers from Connect: {e}", "error")
            return None
        chosen = set(request.form.getlist("printer"))
        for printer in available:
            if printer["uuid"] not in chosen:
                continue
            location = request.form.get(f"location_{printer['uuid']}", "").strip() or printer["name"]
            printers.append(
                {
                    "uuid": printer["uuid"],
                    "organization_id": printer["organization_id"],
                    "name": printer["name"],
                    "location": location[:64],
                    "enabled": True,
                }
            )
        if not printers:
            flash("Choose at least one printer.", "error")
            return None
        update_config(service.settings.data_dir, {"printers": printers})
        return advance("printers")

    def _post_mode():
        try:
            poll_interval = max(10, int(request.form.get("poll_interval", "60")))
            warehouse_interval = max(60, int(request.form.get("warehouse_interval", "600")))
        except ValueError:
            flash("Enter the interval as a whole number of seconds.", "error")
            return None
        update_config(
            service.settings.data_dir,
            {
                "dry_run": request.form.get("dry_run") == "on",
                "auto_create": request.form.get("auto_create") == "on",
                "track_usage": request.form.get("track_usage") == "on",
                "poll_interval": poll_interval,
                "warehouse_interval": warehouse_interval,
            },
        )
        flash("Saved.", "ok")
        service.reload()
        return redirect(url_for("dashboard"))

    return app
