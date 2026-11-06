"""The poller and the state the web interface shows. A lock keeps one Connect call at a time,
because two simultaneous token refreshes could invalidate a rotated refresh token."""

import logging
import os
import threading
import time
from collections import deque
from dataclasses import replace

from .config import ConfigError, Settings, load_settings
from .connect import ConnectAuthError, ConnectClient, TokenStore, Tokens, jwt_expiry
from .spoolman import SpoolmanClient, extra_value
from .sync import (
    SPOOL_UUID_FIELD,
    Bridge,
    LoadedSpool,
    State,
    build_link_changes,
    go_live_step,
    index_spools,
    parse_warehouse_items,
    rank_spools_for_link,
    stored_consumed,
)

log = logging.getLogger(__name__)

MAX_BACKOFF_SECONDS = 1800


class RingBufferHandler(logging.Handler):
    def __init__(self, capacity: int = 200):
        super().__init__()
        self.lines: deque[str] = deque(maxlen=capacity)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


class Service:
    def __init__(self, env: dict | None = None):
        self.env = env
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.thread: threading.Thread | None = None
        self.logs = RingBufferHandler()
        self.settings = Settings()
        self.config_error: str | None = None
        self.spoolman: SpoolmanClient | None = None
        self.connect: ConnectClient | None = None
        self.bridge: Bridge | None = None
        self.status: dict[str, dict] = {}
        self.login_email: str | None = None
        self.last_auth_error: str | None = None
        self.warehouse: list[dict] = []
        self.warehouse_empty: list[dict] = []
        self.warehouse_time: float | None = None
        self.warehouse_error: str | None = None
        self.warehouse_due = 0.0
        self.reload()

    def reload(self) -> None:
        """Read the settings again and rebuild the clients."""
        with self.lock:
            try:
                self.settings = load_settings(self.env)
                self.connect = ConnectClient(self.settings, TokenStore(self.settings.tokens_path))
                self.spoolman = SpoolmanClient(self.settings) if self.settings.spoolman_url else None
                self.config_error = None
            except ConfigError as e:
                log.error("%s", e)
                self.config_error = str(e)
                self.spoolman = None
            self.bridge = None
            self.warehouse_due = 0.0
            if self.spoolman and self.connect:
                state = State(self.settings.state_path, self.settings.dry_run)
                self.bridge = Bridge(self.connect, self.spoolman, state, self.settings.auto_create)
        self.wake.set()

    @property
    def store(self) -> TokenStore:
        return TokenStore(self.settings.tokens_path)

    def token_status(self) -> dict:
        """What the interface may show about the login. Never the tokens themselves."""
        if not self.store.exists():
            return {"logged_in": False}
        try:
            tokens = self.store.load()
        except ConnectAuthError:
            return {"logged_in": False}
        return {
            "logged_in": True,
            "expires": jwt_expiry(tokens.access_token),
            "has_refresh": bool(tokens.refresh_token),
            "email": self.login_email,
            "error": self.last_auth_error,
        }

    def log_in_to_connect(self, access: str, refresh: str) -> dict:
        """Store the tokens only if Connect accepts them."""
        with self.lock:
            previous = self.store.load() if self.store.exists() else None
            self.store.save(Tokens(access, refresh))
            try:
                me = self.connect.me()
            except Exception:
                if previous:
                    self.store.save(previous)
                else:
                    self.store.path.unlink(missing_ok=True)
                raise
            self.login_email = me.get("email")
            self.last_auth_error = None
        self.wake.set()
        return me

    def poll_once(self) -> bool:
        """Poll every enabled printer. Returns False if any of them failed."""
        with self.lock:
            if not self.bridge or not self.settings.enabled_printers or not self.store.exists():
                return True
            ok = True
            for config in self.settings.enabled_printers:
                entry = self.status.setdefault(config.uuid, {"actions": []})
                entry["config"] = config
                before = len(self.spoolman.actions) and self.spoolman.actions[-1]["time"]
                try:
                    entry.update(self.bridge.poll(config))
                    entry["error"] = None
                    self.last_auth_error = None
                except ConnectAuthError as e:
                    entry["error"] = str(e)
                    self.last_auth_error = str(e)
                    log.error("CONNECT LOGIN FAILED: %s", e)
                    return False
                except Exception as e:
                    entry["error"] = str(e)
                    log.error("Polling %s failed: %s", config.name or config.uuid, e)
                    ok = False
                new = [a for a in self.spoolman.actions if a["time"] > before]
                if new:
                    entry["actions"] = (entry["actions"] + new)[-10:]
            self._refresh_warehouse()
            return ok

    def _refresh_warehouse(self) -> None:
        """Reads Connect only, and a failure here must not stop consumption tracking."""
        now = time.time()
        if now < self.warehouse_due:
            return
        self.warehouse_due = now + self.settings.warehouse_interval
        spools = []
        try:
            for organization_id in sorted({p.organization_id for p in self.settings.enabled_printers}):
                spools.extend(parse_warehouse_items(self.connect.warehouse_items(organization_id), include_empty=True))
        except Exception as e:
            self.warehouse_error = str(e)
            log.warning("Could not read the Connect warehouse: %s", e)
            return
        self.warehouse = [entry for entry in spools if not entry["empty"]]
        self.warehouse_empty = [entry for entry in spools if entry["empty"]]
        self.warehouse_time = now
        self.warehouse_error = None

    def refresh_warehouse_soon(self) -> None:
        self.warehouse_due = 0.0
        self.wake.set()

    def warehouse_view(self, spools: list[dict]) -> dict:
        """Which warehouse spools still need a choice. Links are read from Spoolman each time, so this is never stale."""
        linked = {extra_value(spool, SPOOL_UUID_FIELD) for spool in spools}
        unlinked = [entry for entry in self.warehouse if entry["spool"].uuid not in linked]
        printers = {
            tool["spool"].uuid: entry.get("name") or entry["config"].name
            for entry in self.status.values()
            for tool in entry.get("tools", [])
        }
        return {
            "unlinked": [{**entry, "printer": printers.get(entry["spool"].uuid)} for entry in unlinked],
            "linked_count": len(self.warehouse) - len(unlinked),
            "time": self.warehouse_time,
            "error": self.warehouse_error,
        }

    def warehouse_overview(self, spools: list[dict] | None) -> dict:
        """Every spool Connect knows about: the warehouse, plus loaded spools that are not in it.

        spools is the Spoolman list, or None when Spoolman could not be read; links are then unknown.
        """
        by_uuid = {}
        for spool in spools or []:
            uuid = extra_value(spool, SPOOL_UUID_FIELD)
            if uuid:
                by_uuid[uuid] = spool
        loaded = {}
        for entry in self.status.values():
            for tool in entry.get("tools", []):
                loaded[tool["spool"].uuid] = (tool["spool"], entry.get("name") or entry["config"].name or entry["config"].uuid)

        rows = []

        def add(spool: LoadedSpool, location: str, empty: bool, in_warehouse: bool) -> None:
            printer = loaded.get(spool.uuid, (None, None))[1]
            linked = by_uuid.get(spool.uuid)
            rows.append({
                "spool": spool,
                "location": location,
                "printer": printer,
                "empty": empty,
                "in_warehouse": in_warehouse,
                "linked": linked and {
                    "id": linked["id"],
                    "url": self.spoolman.spool_url(linked["id"]),
                    "remaining": linked.get("remaining_weight"),
                },
            })

        in_warehouse = set()
        for entry in self.warehouse + self.warehouse_empty:
            spool = entry["spool"]
            in_warehouse.add(spool.uuid)
            # A loaded spool's tool reports live consumption, which is newer than the warehouse read.
            add(loaded.get(spool.uuid, (spool,))[0], entry["location"], entry["empty"], True)
        for uuid, (spool, _printer) in loaded.items():
            if uuid not in in_warehouse:
                add(spool, "", False, False)

        rows.sort(key=lambda r: (r["empty"], r["printer"] is None, (r["spool"].material_type or "~").upper(), r["spool"].display_name.lower()))
        active = [r for r in rows if not r["empty"]]
        materials = {}
        for row in active:
            key = row["spool"].material_type or "Unknown"
            count, grams = materials.get(key, (0, 0.0))
            materials[key] = (count + 1, grams + (row["spool"].remaining_weight or 0))
        return {
            "rows": rows,
            "links_known": spools is not None,
            "active_count": len(active),
            "empty_count": len(rows) - len(active),
            "loaded_count": sum(1 for r in active if r["printer"]),
            "unlinked_count": sum(1 for r in active if not r["linked"]) if spools is not None else None,
            "remaining_total": sum(r["spool"].remaining_weight or 0 for r in active),
            "unknown_weight_count": sum(1 for r in active if r["spool"].remaining_weight is None),
            "materials": sorted(materials.items(), key=lambda item: -item[1][1]),
            "time": self.warehouse_time,
            "error": self.warehouse_error,
        }

    def go_live_preview(self) -> dict:
        """What turning dry run off would deduct on the first live poll, from the last poll's numbers."""
        with self.lock:
            # Looked up by instance UUID, like the live poll, so a link changed since the last poll is not missed.
            spools = index_spools(self.spoolman.list_spools())
            # A live bridge reads the state file without the dry run's in-memory values.
            state = State(self.settings.state_path, dry_run=False)
            rows = []
            unlinked = []
            for config in self.settings.enabled_printers:
                entry = self.status.get(config.uuid) or {}
                for tool in entry.get("tools", []):
                    loaded = tool["spool"]
                    spool = spools.get(loaded.uuid)
                    if spool is None:
                        unlinked.append({"spool": loaded, "printer": entry.get("name") or config.name})
                        continue
                    stored = stored_consumed(spool)
                    previous = stored if stored is not None else state.previous(loaded.uuid)
                    rows.append({"spool": loaded, "spool_id": spool["id"], "printer": entry.get("name") or config.name, **go_live_step(loaded, previous)})
            polled = [self.status[c.uuid]["time"] for c in self.settings.enabled_printers if (self.status.get(c.uuid) or {}).get("time")]
        return {"rows": rows, "unlinked": unlinked, "total": sum(r["deduct"] for r in rows), "time": min(polled) if polled else None}

    def _loop(self) -> None:
        failures = 0
        while not self.stopping.is_set():
            self.wake.clear()
            if self.poll_once():
                failures = 0
                delay = self.settings.poll_interval
            else:
                failures += 1
                delay = min(self.settings.poll_interval * 2**failures, MAX_BACKOFF_SECONDS)
                log.info("Next attempt in %d s", delay)
            self.wake.wait(delay)

    def start(self) -> None:
        log.info(
            "Starting version %s; polling every %d s, DRY_RUN=%s, AUTO_CREATE=%s",
            os.environ.get("APP_VERSION", "dev"),
            self.settings.poll_interval, self.settings.dry_run, self.settings.auto_create,
        )
        self.thread = threading.Thread(target=self._loop, name="poller", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stopping.set()
        self.wake.set()

    def poller_alive(self) -> bool:
        return self.thread is None or self.thread.is_alive()

    def find_loaded(self, uuid: str) -> tuple[LoadedSpool, str] | None:
        for entry in self.status.values():
            for tool in entry.get("tools", []):
                if tool["spool"].uuid == uuid:
                    return tool["spool"], entry["config"].location
        for entry in self.warehouse:
            if entry["spool"].uuid == uuid:
                return entry["spool"], entry["location"]
        return None

    def _other_locations(self, loaded: LoadedSpool) -> tuple[str, ...]:
        return tuple(
            entry["config"].location
            for entry in self.status.values()
            if entry["config"].location and all(tool["spool"].uuid != loaded.uuid for tool in entry.get("tools", []))
        )

    def _loaded_now(self) -> set[str]:
        return {tool["spool"].uuid for entry in self.status.values() for tool in entry.get("tools", [])}

    def list_spools(self) -> list[dict]:
        with self.lock:
            return self.spoolman.list_spools()

    def link_candidates(self, loaded: LoadedSpool, spools: list[dict]) -> list[dict]:
        with self.lock:
            return rank_spools_for_link(loaded, spools, self._other_locations(loaded), self._loaded_now())

    def _loaded(self, uuid: str) -> tuple[LoadedSpool, str]:
        found = self.find_loaded(uuid)
        if not found:
            raise ValueError("That spool is no longer available")
        return found

    def link_review(self, uuid: str, spool_id: int) -> dict:
        with self.lock:
            loaded, _ = self._loaded(uuid)
            spools = self.spoolman.list_spools()
            spool = next((s for s in spools if s["id"] == spool_id), None)
            if spool is None:
                raise ValueError(f"Spool {spool_id} was not found")
            filament_id = (spool.get("filament") or {}).get("id")
            shared = sum(
                1
                for other in spools
                if other["id"] != spool_id and filament_id is not None and (other.get("filament") or {}).get("id") == filament_id
            )
            return {"loaded": loaded, "spool": spool, "shared": shared, "changes": build_link_changes(loaded, spool, shared)}

    def _capture(self, action) -> list[str]:
        self.spoolman.captured.clear()
        action()
        calls = list(self.spoolman.captured)
        self.wake.set()
        return calls

    def link(self, uuid: str, spool_id: int, selected: set[str]) -> list[str]:
        with self.lock:
            loaded, _ = self._loaded(uuid)
            return self._capture(lambda: self.bridge.link_spool(loaded, spool_id, selected, self._loaded_now()))

    def unlink(self, spool_id: int) -> list[str]:
        with self.lock:
            return self._capture(lambda: self.bridge.unlink_spool(spool_id))

    def create_review(self, uuid: str) -> dict:
        with self.lock:
            loaded, location = self._loaded(uuid)
            if not loaded.can_create:
                raise ValueError("Connect did not report enough about this spool to create it")
            return {"loaded": loaded, "location": location, "preview": self.bridge.create_preview(loaded)}

    def create(self, uuid: str, name: str, location: str) -> list[str]:
        with self.lock:
            loaded, _ = self._loaded(uuid)
            named = replace(loaded, material_name=name or loaded.material_name)
            return self._capture(lambda: self.bridge.create_spool(named, location))
