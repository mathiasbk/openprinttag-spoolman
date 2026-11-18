"""Self-hosted Prusa Connect: the server Prusa publishes at github.com/prusa3d/prusa-connect.

The printer reports to a server on your own network, and its Materials extension keeps the spools.
This client reads them over that server's REST API with an API credential and answers in the same
shapes as the cloud client, so the rest of the bridge does not care which one it talks to.

The self-hosted server is in beta, so its API can still change between releases.
"""

import logging
import ssl

import httpx

from .config import ConfigError, Settings
from .connect import USER_AGENT, ConnectAuthError, ConnectError

log = logging.getLogger(__name__)

MATERIALS_NAMESPACE = "com.github.aux.prusa3d.materials"
LOCAL_ORGANIZATION_ID = "local"
LOCAL_ORGANIZATION_NAME = "Self-hosted Connect"
PAGE_SIZE = 100
MAX_PAGES = 50


def _problem(response: httpx.Response) -> str:
    """The title of a problem document, or the status code when there is none."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and (body.get("title") or body.get("code")):
        return f"{body.get('title') or body.get('code')} (HTTP {response.status_code})"
    return f"HTTP {response.status_code}"


def _consumed(full: float | None, remaining: float | None) -> float | None:
    if full is None or remaining is None:
        return None
    return max(0.0, float(full) - float(remaining))


def _diameter(spool: dict) -> float | None:
    micrometres = spool.get("diameter_um")
    return micrometres / 1000 if micrometres else None


def _material(spool: dict, tool: dict | None = None) -> dict:
    """The material of a spool in the cloud's shape. What the tool reports fills in what the spool lacks."""
    tool = tool or {}
    return {
        "uuid": spool.get("material_uuid") or tool.get("material_uuid"),
        "name": spool.get("material_name") or tool.get("material_name") or None,
        "abbreviation": tool.get("abbreviation") or None,
        "brand": {"name": spool.get("brand_name") or tool.get("brand_name") or None},
        "primaryColor": {"colorRgba": spool.get("color_rgba") or tool.get("color_rgba") or None},
        "type": {"abbreviation": spool.get("type") or tool.get("type") or None},
        "properties": {"density": spool.get("density") or None},
    }


def tool_instance(tool: dict, spool: dict | None) -> dict:
    """One tool of GET /printers/tools as the cloud's materialPackageInstance."""
    if not tool.get("spool_uuid"):
        return {"__typename": "ToolMaterialPackageInstanceEmpty", "reason": "No spool is assigned in Materials"}
    spool = spool or {}
    full = tool.get("full_weight_g") if tool.get("full_weight_g") is not None else spool.get("full_weight_g")
    remaining = tool.get("remaining_weight_g") if tool.get("remaining_weight_g") is not None else spool.get("remaining_weight_g")
    return {
        "__typename": "FFFToolMaterialPackageInstance",
        "uuid": tool["spool_uuid"],
        "actualNettoFullWeight": full,
        "isFromNfc": bool(tool.get("from_nfc")),
        "dynamicData": {"consumedWeight": _consumed(full, remaining)},
        "package": {
            "nominalNettoFullWeight": full,
            "filamentDiameter": _diameter(spool),
            "container": {"emptyWeight": spool.get("empty_weight_g")},
            "material": _material(spool, tool),
        },
    }


def warehouse_item(spool: dict) -> dict:
    """One spool of GET /spools as a node of the cloud's warehouse."""
    full = spool.get("full_weight_g")
    remaining = spool.get("remaining_weight_g")
    material = _material(spool)
    return {
        "name": spool.get("material_name") or None,
        "consumedWeight": _consumed(full, remaining),
        "nominalNettoFullWeight": full,
        "empty": remaining is not None and remaining <= 0,
        "archived": bool(spool.get("archived_at")),
        "location": spool.get("location") or "",
        "materialPackageInstanceUuidSource": "nfc" if spool.get("source") == "printer" else "manual",
        "materialPackageInstance": {
            "uuid": spool.get("uuid"),
            "actualNettoFullWeight": full,
            "package": {
                "filamentDiameter": _diameter(spool),
                "container": {"emptyWeight": spool.get("empty_weight_g")},
                "material": {
                    "uuid": material["uuid"],
                    "type": material["type"]["abbreviation"],
                    "primaryColor": material["primaryColor"],
                    "properties": material["properties"],
                    "brand": material["brand"],
                },
            },
        },
    }


def local_tls_verify(settings: Settings) -> ssl.SSLContext | bool:
    if not settings.local_connect_url.startswith("https://"):
        return True
    if not settings.local_connect_verify_tls:
        log.warning("LOCAL_CONNECT_VERIFY_TLS=false: the self-hosted Connect certificate is NOT being verified")
        return False
    if settings.local_connect_ca_bundle:
        try:
            return ssl.create_default_context(cafile=settings.local_connect_ca_bundle)
        except (OSError, ssl.SSLError) as e:
            raise ConfigError(f"Cannot load LOCAL_CONNECT_CA_BUNDLE {settings.local_connect_ca_bundle}: {e}") from e
    return True


class LocalConnectClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self.settings = settings
        if http is None:
            http = httpx.Client(timeout=30, headers={"User-Agent": USER_AGENT}, verify=local_tls_verify(settings))
        self.http = http
        self._add_problems: dict[tuple[str, str], str] = {}

    @property
    def base(self) -> str:
        return f"{self.settings.local_connect_url}/api/v1/modules/{MATERIALS_NAMESPACE}"

    def _get(self, path: str, params: dict | None = None) -> dict:
        if not self.settings.local_connect_url or not self.settings.local_connect_token:
            raise ConnectAuthError("The self-hosted Connect address or API credential is missing")
        try:
            response = self.http.get(
                f"{self.base}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self.settings.local_connect_token}"},
            )
        except httpx.HTTPError as e:
            raise ConnectError(f"Request to the self-hosted Connect failed: {type(e).__name__}") from e
        if response.status_code in (401, 403):
            raise ConnectAuthError(f"The self-hosted Connect refused the API credential: {_problem(response)}")
        if response.status_code == 404:
            raise ConnectError(
                f"The self-hosted Connect has no Materials extension at {self.base} ({_problem(response)}). "
                "Install it from the marketplace and switch it on"
            )
        if response.status_code != 200:
            raise ConnectError(f"The self-hosted Connect answered {_problem(response)}")
        try:
            body = response.json()
        except ValueError as e:
            raise ConnectError("The self-hosted Connect did not answer with JSON") from e
        if not isinstance(body, dict):
            raise ConnectError("The self-hosted Connect answered with an unexpected shape")
        return body

    def _pages(self, path: str, key: str, params: dict | None = None, page_size: int = PAGE_SIZE) -> list[dict]:
        items = []
        offset = 0
        for _ in range(MAX_PAGES):
            body = self._get(path, {**(params or {}), "offset": offset, "limit": page_size})
            items.extend(body.get(key) or [])
            if body.get("next") is None:
                return items
            offset = body["next"]
        log.warning("Stopped reading %s after %d pages", path, MAX_PAGES)
        return items

    def me(self) -> dict:
        """Checks the address, the credential and the Materials extension with one small read."""
        self._get("/printers", {"limit": 1})
        return {
            "email": None,
            "organizations": [{"id": LOCAL_ORGANIZATION_ID, "name": LOCAL_ORGANIZATION_NAME}],
        }

    def list_printers(self) -> list[dict]:
        return [
            {
                "uuid": printer["id"],
                "name": printer.get("name") or printer["id"],
                "state": printer.get("state") or None,
                "organization_id": LOCAL_ORGANIZATION_ID,
                "organization_name": LOCAL_ORGANIZATION_NAME,
            }
            for printer in self._pages("/printers", "printers")
        ]

    def _spools_of(self, material_uuid: str | None) -> dict[int, dict]:
        params = {"material": material_uuid} if material_uuid else {}
        return {spool["id"]: spool for spool in self._pages("/spools", "spools", params)}

    def fetch_printer(self, organization_id: str, printer_uuid: str) -> dict:
        found = self._get("/printers", {"id": printer_uuid}).get("printers") or []
        printer = next((p for p in found if p.get("id") == printer_uuid), None)
        if printer is None:
            raise ConnectError(f"Printer query failed: the self-hosted Connect does not list {printer_uuid}")
        rows = [row for row in self._pages("/printers/tools", "tools") if row.get("device_id") == printer_uuid]
        for row in rows:
            if row.get("from_nfc") and not row.get("spool_uuid"):
                self._add_tagged_spool(printer_uuid, row)
        spools: dict[int, dict] = {}
        for material_uuid in {row.get("material_uuid") for row in rows if row.get("spool_id") is not None}:
            spools.update(self._spools_of(material_uuid))
        tools = [
            {"id": row.get("tool_id"), "materialPackageInstance": tool_instance(row, spools.get(row.get("spool_id")))}
            for row in rows
        ]
        return {
            "__typename": "Printer",
            "id": printer_uuid,
            "name": printer.get("name") or printer_uuid,
            "state": printer.get("state") or None,
            "fff": {"tools": tools},
        }

    def _add_tagged_spool(self, printer_uuid: str, row: dict) -> None:
        """Puts the spool a tool read off its NFC tag into Materials' stock, so the tool names it.

        Materials links a tag to a tool only after "Add to Materials". Without that a newly
        loaded tagged spool reads as no spool at all. The call is idempotent: a tag a spool
        already carries answers that spool. A failure leaves the tool empty for this poll.
        """
        tool_id = row.get("tool_id")
        tool_key = (printer_uuid, str(tool_id))
        url = f"{self.base}/printers/{printer_uuid}/tools/{tool_id}/spool"
        try:
            response = self.http.post(url, headers={"Authorization": f"Bearer {self.settings.local_connect_token}"})
        except httpx.HTTPError as e:
            self._add_failed(tool_key, f"Could not add the tagged spool on tool {tool_id} to Materials: {type(e).__name__}")
            return
        if response.status_code in (401, 403):
            self._add_failed(
                tool_key,
                f"The API credential may not add the tagged spool on tool {tool_id} to Materials ({_problem(response)}). "
                "That takes a credential with the operate scope. With read, click Add to Materials in the server yourself",
            )
            return
        if response.status_code not in (200, 201):
            self._add_failed(tool_key, f"Materials refused to add the tagged spool on tool {tool_id}: {_problem(response)}")
            return
        try:
            added = response.json()
        except ValueError:
            self._add_failed(tool_key, f"Materials answered the spool add on tool {tool_id} without JSON")
            return
        if not isinstance(added, dict) or not added.get("uuid"):
            self._add_failed(tool_key, f"Materials answered the spool add on tool {tool_id} with no spool")
            return
        self._add_problems.pop(tool_key, None)
        log.info("Added the tagged spool %s on tool %s to Materials", added["uuid"], tool_id)
        row.update(spool_id=added.get("id"), spool_uuid=added["uuid"])
        for key in ("remaining_weight_g", "full_weight_g"):
            if row.get(key) is None and added.get(key) is not None:
                row[key] = added[key]

    def _add_failed(self, key: tuple[str, str], message: str) -> None:
        """Warns once per tool and problem. The add is retried every poll, so repeats go to the debug log."""
        if self._add_problems.get(key) == message:
            log.debug(message)
            return
        self._add_problems[key] = message
        log.warning(message)

    def warehouse_items(self, organization_id: str) -> list[dict]:
        return [warehouse_item(spool) for spool in self._pages("/spools", "spools") if spool.get("uuid")]
