"""Spoolman REST client. All writes pass through _write, which honours DRY_RUN."""

import json
import logging
import ssl
import time
from collections import deque

import httpx

from .config import ConfigError, Settings

log = logging.getLogger(__name__)


class SpoolmanError(Exception):
    pass


def tls_verify(settings: Settings) -> ssl.SSLContext | bool:
    if settings.spoolman_url.startswith("http://"):
        return True
    if not settings.spoolman_verify_tls:
        log.warning("SPOOLMAN_VERIFY_TLS=false: the Spoolman certificate is NOT being verified")
        return False
    if settings.spoolman_ca_bundle:
        try:
            return ssl.create_default_context(cafile=settings.spoolman_ca_bundle)
        except (OSError, ssl.SSLError) as e:
            raise ConfigError(f"Cannot load SPOOLMAN_CA_BUNDLE {settings.spoolman_ca_bundle}: {e}") from e
    return True


def extra_value(entity: dict, key: str):
    """Spoolman stores extra field values as JSON-encoded strings."""
    raw = (entity.get("extra") or {}).get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return raw


def encode_extra(value: str) -> str:
    return json.dumps(value)


def spool_label(spool: dict) -> str:
    """'#55 (ELEGOO PETG-GF Black)', for messages people read."""
    filament = spool.get("filament") or {}
    name = " ".join(part for part in ((filament.get("vendor") or {}).get("name"), filament.get("name")) if part)
    return f"#{spool['id']} ({name})" if name else f"#{spool['id']}"


def describe(method: str, path: str) -> str:
    """A plain-language fallback for a write that has no summary of its own."""
    parts = path.strip("/").split("/")
    what = {"spool": "spool", "filament": "filament", "vendor": "vendor", "field": "extra field"}.get(parts[0], parts[0])
    target = f"{what} #{parts[1]}" if len(parts) > 1 and parts[1].isdigit() else what
    verb = {"POST": "Created", "PATCH": "Updated", "PUT": "Updated", "DELETE": "Deleted"}.get(method, "Changed")
    return f"{verb} {target} in Spoolman"


class SpoolmanClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.dry_run = settings.dry_run
        self.url = settings.spoolman_url
        self.base = f"{settings.spoolman_url}/api/v1"
        self.http = http or httpx.Client(timeout=30, verify=tls_verify(settings))
        self.actions: deque[dict] = deque(maxlen=50)
        self._announced: set[str] = set()
        self.captured: list[dict] = []

    def _request(self, method: str, path: str, body: dict | None = None):
        try:
            response = self.http.request(method, f"{self.base}{path}", json=body)
        except httpx.HTTPError as e:
            raise SpoolmanError(f"{method} {path} failed: {e}") from e
        if response.status_code >= 400:
            raise SpoolmanError(f"{method} {path} returned HTTP {response.status_code}: {response.text[:300]}")
        try:
            return response.json() if response.content else None
        except ValueError as e:
            raise SpoolmanError(f"{method} {path} did not return JSON; is this Spoolman?") from e

    def _read(self, path: str):
        return self._request("GET", path)

    def _write(self, method: str, path: str, body: dict, summary: str | None = None):
        """Returns the response, or None in dry-run mode. summary says in plain words what the write does."""
        rendered = json.dumps(body, ensure_ascii=False)
        line = f"{method} {path} {rendered}"
        action = {"dry_run": self.dry_run, "summary": summary or describe(method, path), "text": line}
        if self.dry_run:
            self.captured.append(action)
            # A dry run repeats the same intent every poll; once in the log is enough.
            if line not in self._announced:
                self._announced.add(line)
                log.info("DRY-RUN would call %s", line)
                self.actions.append({"time": time.time(), **action})
            return None
        log.info("Calling %s", line)
        result = self._request(method, path, body)
        # Only writes Spoolman accepted are listed, so a retried write does not show up twice.
        self.captured.append(action)
        self.actions.append({"time": time.time(), **action})
        return result

    def info(self) -> dict:
        return self._read("/info")

    def spool_url(self, spool_id: int) -> str:
        return f"{self.url}/spool/show/{spool_id}"

    def extra_field_exists(self, entity_type: str, key: str) -> bool:
        return any(f.get("key") == key for f in self._read(f"/field/{entity_type}"))

    def create_extra_field(self, entity_type: str, key: str, name: str, field_type: str = "text") -> None:
        self._write("POST", f"/field/{entity_type}/{key}", {"name": name, "field_type": field_type}, f"Created the {entity_type} extra field {key}")

    def list_spools(self) -> list[dict]:
        return self._read("/spool?allow_archived=true")

    def get_spool(self, spool_id: int) -> dict:
        return self._read(f"/spool/{spool_id}")

    def list_vendors(self) -> list[dict]:
        return self._read("/vendor")

    def list_filaments(self) -> list[dict]:
        return self._read("/filament")

    def create_vendor(self, body: dict, summary: str | None = None) -> dict | None:
        return self._write("POST", "/vendor", body, summary)

    def create_filament(self, body: dict, summary: str | None = None) -> dict | None:
        return self._write("POST", "/filament", body, summary)

    def create_spool(self, body: dict, summary: str | None = None) -> dict | None:
        return self._write("POST", "/spool", body, summary)

    def update_spool(self, spool_id: int, body: dict, summary: str | None = None) -> None:
        self._write("PATCH", f"/spool/{spool_id}", body, summary)

    def update_filament(self, filament_id: int, body: dict, summary: str | None = None) -> None:
        self._write("PATCH", f"/filament/{filament_id}", body, summary)

    def use_spool(self, spool_id: int, weight: float) -> None:
        self._write("PUT", f"/spool/{spool_id}/use", {"use_weight": weight}, f"Deducted {weight:g} g from spool #{spool_id}")
