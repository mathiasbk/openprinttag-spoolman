"""One poll: read a printer's loaded spools from Connect and bring Spoolman up to date."""

import json
import logging
import os
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from .config import PrinterConfig
from .connect import ConnectError
from .spoolman import SpoolmanClient, SpoolmanError, encode_extra, extra_value

log = logging.getLogger(__name__)

SPOOL_UUID_FIELD = "opt_instance_uuid"
FILAMENT_UUID_FIELD = "opt_material_uuid"
# The consumed weight the spool's used_weight already accounts for. Kept on the spool in Spoolman,
# not only in state.json, so a second bridge, a reinstall or a lost data folder cannot deduct the
# same grams twice.
CONSUMED_FIELD = "opt_consumed_weight"
WEIGHT_EPSILON = 0.001
USED_WEIGHT_TOLERANCE = 1
HIGH_CONFIDENCE = 70
SUGGESTION_THRESHOLD = 45


@dataclass(frozen=True)
class LoadedSpool:
    tool_id: str
    uuid: str
    is_from_nfc: bool
    actual_weight: float | None
    consumed_weight: float | None
    nominal_weight: float | None
    empty_weight: float | None
    filament_diameter: float | None
    material_uuid: str | None
    material_name: str | None
    material_type: str | None
    brand: str | None
    color_hex: str | None
    density: float | None
    from_warehouse: bool = False

    @property
    def display_name(self) -> str:
        return f"{self.brand or 'Unknown brand'} {self.material_name or self.material_type or 'unknown material'}"

    @property
    def can_create(self) -> bool:
        return all(v is not None for v in (self.brand, self.material_name, self.density, self.filament_diameter))

    @property
    def full_weight(self) -> float | None:
        """A spool from the Warehouse has no measured weight, only the nominal one."""
        return self.actual_weight if self.actual_weight is not None else self.nominal_weight

    @property
    def remaining_weight(self) -> float | None:
        if self.full_weight is None:
            return None
        return max(0.0, self.full_weight - (self.consumed_weight or 0))

    @property
    def remaining_fraction(self) -> float:
        full = self.nominal_weight or self.actual_weight or 0
        if not full or self.full_weight is None:
            return 0.0
        return max(0.0, min(1.0, (self.full_weight - (self.consumed_weight or 0)) / full))

    @property
    def is_missing_details(self) -> bool:
        return any(v is None for v in (self.brand, self.material_name, self.density, self.empty_weight))


def color_hex(rgba: str | None) -> str | None:
    """'#212929' or '#212929FF' becomes '212929'."""
    if not rgba:
        return None
    value = rgba.lstrip("#")
    if len(value) not in (6, 8):
        return None
    try:
        int(value, 16)
    except ValueError:
        return None
    return value[:6].upper()


def parse_loaded_spools(printer: dict) -> list[LoadedSpool]:
    spools = []
    for tool in (printer.get("fff") or {}).get("tools") or []:
        instance = tool.get("materialPackageInstance") or {}
        if instance.get("__typename") != "FFFToolMaterialPackageInstance" or not instance.get("uuid"):
            log.debug("Tool %s has no spool loaded (%s)", tool.get("id"), instance.get("reason") or "no data")
            continue
        package = instance.get("package") or {}
        material = package.get("material") or {}
        spools.append(
            LoadedSpool(
                tool_id=str(tool.get("id")),
                uuid=instance["uuid"],
                is_from_nfc=bool(instance.get("isFromNfc")),
                actual_weight=instance.get("actualNettoFullWeight"),
                consumed_weight=(instance.get("dynamicData") or {}).get("consumedWeight"),
                nominal_weight=package.get("nominalNettoFullWeight"),
                empty_weight=(package.get("container") or {}).get("emptyWeight"),
                filament_diameter=package.get("filamentDiameter"),
                material_uuid=material.get("uuid"),
                material_name=material.get("name"),
                material_type=(material.get("type") or {}).get("abbreviation") or material.get("abbreviation"),
                brand=(material.get("brand") or {}).get("name"),
                color_hex=color_hex((material.get("primaryColor") or {}).get("colorRgba")),
                density=(material.get("properties") or {}).get("density"),
            )
        )
    return spools


def fill_from_warehouse(loaded: LoadedSpool, item: dict) -> LoadedSpool:
    """Adds what the tool did not report. What the tool did report is kept."""
    instance = item.get("materialPackageInstance") or {}
    package = instance.get("package") or {}
    material = package.get("material") or {}
    found = {
        "actual_weight": instance.get("actualNettoFullWeight"),
        "consumed_weight": item.get("consumedWeight"),
        "nominal_weight": item.get("nominalNettoFullWeight"),
        "empty_weight": (package.get("container") or {}).get("emptyWeight"),
        "filament_diameter": package.get("filamentDiameter"),
        "material_uuid": material.get("uuid"),
        "material_name": item.get("name"),
        "material_type": material.get("type"),
        "brand": (material.get("brand") or {}).get("name"),
        "color_hex": color_hex((material.get("primaryColor") or {}).get("colorRgba")),
        "density": (material.get("properties") or {}).get("density"),
    }
    missing = {field: value for field, value in found.items() if getattr(loaded, field) is None}
    return replace(loaded, from_warehouse=True, **missing)


def parse_warehouse_items(items: list[dict], include_empty: bool = False) -> list[dict]:
    """The spools in the Connect warehouse that are still in use, loaded in a printer or not.

    With include_empty, spools marked empty are kept too, with "empty" set. Archived spools are always left out.
    """
    spools = []
    for item in items:
        uuid = (item.get("materialPackageInstance") or {}).get("uuid")
        if not uuid or item.get("archived") or (item.get("empty") and not include_empty):
            continue
        blank = LoadedSpool(
            tool_id="",
            uuid=uuid,
            is_from_nfc=item.get("materialPackageInstanceUuidSource") == "nfc",
            actual_weight=None,
            consumed_weight=None,
            nominal_weight=None,
            empty_weight=None,
            filament_diameter=None,
            material_uuid=None,
            material_name=None,
            material_type=None,
            brand=None,
            color_hex=None,
            density=None,
        )
        spools.append({"spool": fill_from_warehouse(blank, item), "location": item.get("location") or "", "empty": bool(item.get("empty"))})
    return spools


def consumption_step(previous: float | None, current: float) -> tuple[str, float]:
    """Returns (action, weight to deduct), where action is baseline, none, use or rebase."""
    if previous is None:
        return "baseline", 0.0
    delta = current - previous
    if delta > WEIGHT_EPSILON:
        return "use", delta
    if delta < -WEIGHT_EPSILON:
        return "rebase", 0.0
    return "none", 0.0


def index_spools(spools: list[dict]) -> dict[str, dict]:
    index = {}
    for spool in spools:
        uuid = extra_value(spool, SPOOL_UUID_FIELD)
        if not uuid:
            continue
        if uuid in index:
            log.warning("Spools %s and %s share instance UUID %s; using the first", index[uuid]["id"], spool["id"], uuid)
            continue
        index[uuid] = spool
    return index


def duplicate_links(spools: list[dict]) -> dict[str, list[int]]:
    """Instance UUIDs that more than one spool is linked to, with those spools' IDs."""
    by_uuid: dict[str, list[int]] = {}
    for spool in spools:
        uuid = extra_value(spool, SPOOL_UUID_FIELD)
        if uuid:
            by_uuid.setdefault(uuid, []).append(spool["id"])
    return {uuid: ids for uuid, ids in by_uuid.items() if len(ids) > 1}


def stored_consumed(spool: dict) -> float | None:
    """The consumed weight the bridge last accounted for on this spool, if it wrote one."""
    value = extra_value(spool, CONSUMED_FIELD)
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        log.warning("Spool %s has an unreadable %s value %r; ignoring it", spool.get("id"), CONSUMED_FIELD, value)
        return None


def encode_consumed(consumed: float) -> str:
    return json.dumps(round(float(consumed), 3))


def has_openprinttag_tag(spool: dict) -> bool:
    """Spoolman 0.27 can link a physical OpenPrintTag tag to a spool itself, when a reader scans it."""
    return any((tag.get("format") or "").casefold() == "openprinttag" for tag in spool.get("tags") or [])


def implausible_use(loaded: "LoadedSpool", delta: float) -> bool:
    """More than a whole spool in one step means the numbers belong to something else."""
    full = loaded.full_weight or loaded.nominal_weight
    return full is not None and delta > full + USED_WEIGHT_TOLERANCE


def go_live_step(loaded: "LoadedSpool", previous: float | None) -> dict:
    """What the first live poll would do with a linked spool: grams to deduct, and why."""
    current = loaded.consumed_weight
    if current is None:
        return {"previous": previous, "current": None, "deduct": 0.0, "note": "Connect reports no consumption for this spool yet."}
    current = float(current)
    action, delta = consumption_step(previous, current)
    note = None
    if action == "baseline":
        note = f"Not seen before, so counting starts at {round(current)} g."
    elif action == "rebase":
        note = "Connect reports less than last time, so counting starts again from here."
    elif action == "use" and implausible_use(loaded, delta):
        action, delta = "rebase", 0.0
        note = "More than a whole spool since last time, so this is not deducted."
    return {"previous": previous, "current": current, "deduct": delta if action == "use" else 0.0, "note": note}


def _color_distance(a: str | None, b: str | None) -> float | None:
    if not a or not b or len(a) != 6 or len(b) != 6:
        return None
    try:
        return sum(abs(int(a[i : i + 2], 16) - int(b[i : i + 2], 16)) for i in (0, 2, 4)) / (3 * 255)
    except ValueError:
        return None


def name_similarity(a: str | None, b: str | None) -> float:
    if not a or not b:
        return 0.0
    tokens = [" ".join(sorted(name.casefold().split())) for name in (a, b)]
    return SequenceMatcher(None, *tokens).ratio()


def score_spool(loaded: LoadedSpool, spool: dict, other_locations: tuple[str, ...] = ()) -> dict:
    """How likely it is that a Spoolman spool is this physical spool, with the reasons."""
    filament = spool.get("filament") or {}
    vendor = (filament.get("vendor") or {}).get("name") or ""
    material = filament.get("material") or ""
    color = (filament.get("color_hex") or "").upper()[:6]
    score = 0
    reasons = []
    if loaded.material_uuid and extra_value(filament, FILAMENT_UUID_FIELD) == loaded.material_uuid:
        score += 40
        reasons.append("Same OpenPrintTag material")
    if loaded.brand and vendor.casefold() == loaded.brand.casefold():
        score += 25
        reasons.append("Same brand")
    if loaded.material_type and material.casefold() == loaded.material_type.casefold():
        score += 15
        reasons.append("Same material type")
    similarity = name_similarity(loaded.material_name, filament.get("name"))
    if similarity >= 0.6:
        score += round(25 * similarity)
        reasons.append(f"Name {round(similarity * 100)}% similar")
    distance = _color_distance(loaded.color_hex, color)
    if distance is not None and distance < 0.05:
        score += 10
        reasons.append(f"Color very close (#{color} and #{loaded.color_hex})")
    elif distance is not None and distance < 0.15:
        score += 5
        reasons.append(f"Color close (#{color} and #{loaded.color_hex})")
    if loaded.nominal_weight and filament.get("weight") == loaded.nominal_weight:
        score += 5
        reasons.append("Same nominal weight")
    remaining = spool.get("remaining_weight")
    if remaining is not None and loaded.full_weight is not None:
        if abs(remaining - (loaded.full_weight - (loaded.consumed_weight or 0))) <= 50:
            score += 5
            reasons.append("Remaining weight is close")
    if has_openprinttag_tag(spool):
        reasons.append("Has an OpenPrintTag tag in Spoolman")
    if spool.get("location") and spool["location"] in other_locations:
        score -= 15
        reasons.append("Located at another printer")
    if score >= HIGH_CONFIDENCE:
        confidence = "High"
    elif score >= SUGGESTION_THRESHOLD:
        confidence = "Medium"
    else:
        confidence = "Low"
    return {
        "id": spool["id"],
        "label": f"#{spool['id']} {vendor} {filament.get('name') or ''} ({material}, {round(remaining or 0)} g)".replace("  ", " "),
        "color": f"#{color}" if color else "",
        "score": score,
        "confidence": confidence,
        "reasons": reasons,
        "suggested": score >= SUGGESTION_THRESHOLD,
    }


def can_replace_link(loaded: LoadedSpool, linked_to, loaded_now: set[str]) -> bool:
    """A spool without an NFC reading can get a new ID each time it is loaded, so its old link may be stale.

    A warehouse spool keeps its ID, so it must never take over another spool's link.
    """
    return bool(linked_to) and not loaded.is_from_nfc and not loaded.from_warehouse and linked_to not in loaded_now


def rank_spools_for_link(
    loaded: LoadedSpool, spools: list[dict], other_locations: tuple[str, ...] = (), loaded_now: set[str] | None = None
) -> list[dict]:
    """Candidates for an unlinked spool, best first. Nothing is linked until the user confirms."""
    loaded_now = loaded_now or set()
    ranked = [
        score_spool(loaded, spool, other_locations)
        for spool in spools
        if not spool.get("archived")
        and (not extra_value(spool, SPOOL_UUID_FIELD) or can_replace_link(loaded, extra_value(spool, SPOOL_UUID_FIELD), loaded_now))
    ]
    ranked.sort(key=lambda c: (-c["score"], c["id"]))
    for position, candidate in enumerate(ranked):
        candidate["best"] = position == 0 and candidate["suggested"]
    return ranked


def _differs(before, after) -> bool:
    if isinstance(before, (int, float)) and isinstance(after, (int, float)):
        return abs(before - after) > WEIGHT_EPSILON
    return before != after


def build_link_changes(loaded: LoadedSpool, spool: dict, shared: int) -> list[dict]:
    """The rows on the review page. Filament changes are off if other spools share the filament."""
    filament = spool.get("filament") or {}
    # Without an NFC reading, Connect's material data may be generic, so it should not overwrite anything by default.
    reliable = loaded.is_from_nfc or loaded.from_warehouse
    filament_on = shared == 0 and reliable
    # Without a material UUID, nothing can find this filament again, so setting one is safe even when it is shared.
    material_on = filament_on or (reliable and extra_value(filament, FILAMENT_UUID_FIELD) is None)
    rows = [
        ("spool_instance", "spool", SPOOL_UUID_FIELD, "Link to this tag", extra_value(spool, SPOOL_UUID_FIELD), loaded.uuid, True),
        ("spool_initial_weight", "spool", "initial_weight", "Spool: filament weight", spool.get("initial_weight"), loaded.full_weight, loaded.actual_weight is not None),
        ("spool_spool_weight", "spool", "spool_weight", "Spool: empty weight", spool.get("spool_weight"), loaded.empty_weight, True),
        ("spool_used_weight", "spool", "used_weight", "Spool: consumed", spool.get("used_weight"), loaded.consumed_weight, True),
        ("filament_name", "filament", "name", "Filament: name", filament.get("name"), loaded.material_name, filament_on),
        ("filament_color_hex", "filament", "color_hex", "Filament: color", (filament.get("color_hex") or "").upper() or None, loaded.color_hex, filament_on),
        ("filament_weight", "filament", "weight", "Filament: nominal weight", filament.get("weight"), loaded.nominal_weight, filament_on),
        ("filament_spool_weight", "filament", "spool_weight", "Filament: empty weight", filament.get("spool_weight"), loaded.empty_weight, filament_on),
        ("filament_material", "filament", FILAMENT_UUID_FIELD, "Filament: OpenPrintTag material", extra_value(filament, FILAMENT_UUID_FIELD), loaded.material_uuid, material_on),
    ]
    used = spool.get("used_weight")
    consumed = loaded.consumed_weight
    more_used_elsewhere = used is not None and consumed is not None and used > consumed + USED_WEIGHT_TOLERANCE
    changes = []
    for key, target, field, label, before, after, checked in rows:
        if after is None or not _differs(before, after):
            continue
        note = None
        if key == "spool_used_weight" and more_used_elsewhere:
            checked = False
            note = f"Spoolman has recorded more usage ({round(used)} g) than the tag ({round(consumed)} g). It was likely used on another printer, so Spoolman's value is kept by default."
        changes.append(
            {"key": key, "target": target, "field": field, "label": label, "before": before, "after": after, "checked": checked, "locked": key == "spool_instance", "note": note}
        )
    return changes


class State:
    """Last seen consumed weight per instance UUID.

    A dry run keeps only its first baseline on disk, under dry_baseline. Going live
    continues from there, so consumption during the dry run is still deducted.
    """

    def __init__(self, path: Path, dry_run: bool):
        self.path = path
        self.dry_run = dry_run
        self.consumed: dict[str, float] = {}
        self.dry_baseline: dict[str, float] = {}
        self._seen: dict[str, float] = {}
        try:
            data = json.loads(path.read_text())
            self.consumed = data.get("consumed", {})
            self.dry_baseline = data.get("dry_baseline", {})
        except FileNotFoundError:
            pass
        except ValueError:
            log.error("%s is unreadable; starting from new baselines", path)

    def seen_in_dry_run(self, uuid: str) -> bool:
        return self.dry_run and uuid in self._seen

    def previous(self, uuid: str) -> float | None:
        if self.dry_run and uuid in self._seen:
            return self._seen[uuid]
        return self.consumed.get(uuid, self.dry_baseline.get(uuid))

    def record(self, uuid: str, consumed: float, action: str = "use") -> None:
        if self.dry_run:
            self._seen[uuid] = consumed
            if action in ("baseline", "rebase") and uuid not in self.consumed:
                self.dry_baseline[uuid] = consumed
                self._save()
            return
        self.consumed[uuid] = consumed
        self.dry_baseline.pop(uuid, None)
        self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"consumed": self.consumed, "dry_baseline": self.dry_baseline}, indent=2))
        os.replace(tmp, self.path)


class Bridge:
    def __init__(self, connect, spoolman: SpoolmanClient, state: State, auto_create: bool = False):
        self.connect = connect
        self.spoolman = spoolman
        self.state = state
        self.auto_create = auto_create
        self.fields_ready = False
        self._warned_unlinked: set[str] = set()
        self._placed: dict[str, set[str]] = {}
        self._warned_moved: set[str] = set()
        self._warned_duplicates: set[str] = set()

    def poll(self, printer_config: PrinterConfig) -> dict:
        printer = self.connect.fetch_printer(printer_config.organization_id, printer_config.uuid)
        loaded = self._with_warehouse_details(parse_loaded_spools(printer), printer_config.organization_id)
        log.info("Printer %s is %s with %d spool(s) loaded", printer.get("name"), printer.get("state"), len(loaded))
        self._ensure_extra_fields()
        all_spools = self.spoolman.list_spools()
        spools = index_spools(all_spools)
        warnings = self._duplicate_warnings(all_spools, loaded)
        # A spool that was taken out gets its location set again the next time it is loaded.
        placed = self._placed.setdefault(printer_config.uuid, set())
        taken_out = placed - {spool.uuid for spool in loaded}
        placed -= taken_out
        self._warned_moved -= taken_out
        tools = []
        for spool in loaded:
            matched = spools.get(spool.uuid)
            spool_id = self._sync_spool(spool, matched, printer_config.location, placed, all_spools, warnings)
            tools.append({"spool": spool, "spool_id": spool_id})
        return {"name": printer.get("name"), "state": printer.get("state"), "tools": tools, "warnings": warnings, "time": time.time()}

    def _duplicate_warnings(self, spools: list[dict], loaded: list[LoadedSpool]) -> list[str]:
        """Two spools linked to one loaded tag means one of them gets no consumption."""
        warnings = []
        loaded_now = {spool.uuid for spool in loaded}
        for uuid, ids in duplicate_links(spools).items():
            if uuid not in loaded_now:
                continue
            ids_text = ", ".join(f"#{i}" for i in ids)
            warnings.append(
                f"Spools {ids_text} are all linked to the loaded tag {uuid}. Only #{ids[0]} gets the consumption. Unlink the duplicates."
            )
            if uuid not in self._warned_duplicates:
                self._warned_duplicates.add(uuid)
                log.warning("Spools %s are all linked to instance %s", ids_text, uuid)
        return warnings

    def _with_warehouse_details(self, loaded: list[LoadedSpool], organization_id: str) -> list[LoadedSpool]:
        if not any(spool.is_missing_details for spool in loaded):
            return loaded
        try:
            items = self.connect.warehouse_items(organization_id)
        except ConnectError as e:
            # Consumption can still be tracked without these details.
            log.warning("Could not read the Connect warehouse: %s", e)
            return loaded
        by_uuid = {(item.get("materialPackageInstance") or {}).get("uuid"): item for item in items}
        return [fill_from_warehouse(spool, by_uuid[spool.uuid]) if spool.uuid in by_uuid else spool for spool in loaded]

    def _ensure_extra_fields(self) -> None:
        if self.fields_ready:
            return
        if not self.spoolman.extra_field_exists("spool", SPOOL_UUID_FIELD):
            self.spoolman.create_extra_field("spool", SPOOL_UUID_FIELD, "OpenPrintTag instance UUID")
        if not self.spoolman.extra_field_exists("spool", CONSUMED_FIELD):
            self.spoolman.create_extra_field("spool", CONSUMED_FIELD, "OpenPrintTag consumed (g), set by the bridge", "float")
        if not self.spoolman.extra_field_exists("filament", FILAMENT_UUID_FIELD):
            self.spoolman.create_extra_field("filament", FILAMENT_UUID_FIELD, "OpenPrintTag material UUID")
        self.fields_ready = not self.spoolman.dry_run

    def _sync_spool(
        self, loaded: LoadedSpool, spool: dict | None, location: str, placed: set[str], all_spools: list[dict] | None = None, warnings: list[str] | None = None
    ) -> int | None:
        log.info(
            "Tool %s: %s %s, instance %s, actual %s g, consumed %s g, NFC %s",
            loaded.tool_id, loaded.brand, loaded.material_name, loaded.uuid,
            loaded.actual_weight, loaded.consumed_weight, loaded.is_from_nfc,
        )
        if spool is None:
            if self.auto_create and loaded.is_from_nfc:
                existing = self._likely_existing_spool(loaded, all_spools or [])
                if existing is None:
                    created = self.create_spool(loaded, location)
                    return created["id"] if created else None
                message = (
                    f"Did not create a new spool for {loaded.display_name}: Spoolman spool {existing['label']} looks like the same spool "
                    f"({', '.join(existing['reasons'])}). Link it instead, or create a new one from the dashboard."
                )
                if warnings is not None:
                    warnings.append(message)
                if loaded.uuid not in self._warned_unlinked:
                    self._warned_unlinked.add(loaded.uuid)
                    log.warning("%s", message)
                return None
            if loaded.uuid not in self._warned_unlinked:
                self._warned_unlinked.add(loaded.uuid)
                log.warning("Instance %s is not linked to a Spoolman spool; waiting for a choice (NFC %s)", loaded.uuid, loaded.is_from_nfc)
            return None
        log.info("Instance %s is Spoolman spool %s", loaded.uuid, spool["id"])
        if spool.get("archived"):
            log.warning("Spool %s is archived in Spoolman but loaded in the printer", spool["id"])
        self._sync_location(loaded, spool, location, placed)
        self._sync_consumption(loaded, spool)
        return spool["id"]

    @staticmethod
    def _likely_existing_spool(loaded: LoadedSpool, spools: list[dict]) -> dict | None:
        """An unlinked spool that is very likely this one, for example one Spoolman created when its own reader scanned the tag."""
        candidates = [
            score_spool(loaded, spool)
            for spool in spools
            if not spool.get("archived") and not extra_value(spool, SPOOL_UUID_FIELD)
        ]
        strong = [c for c in candidates if c["score"] >= HIGH_CONFIDENCE]
        strong.sort(key=lambda c: (-c["score"], c["id"]))
        return strong[0] if strong else None

    def _sync_location(self, loaded: LoadedSpool, spool: dict, location: str, placed: set[str]) -> None:
        """Set the location once per load, so the bridge never fights over it with something else that writes it."""
        if not location or spool.get("location") == location:
            return
        if loaded.uuid not in placed:
            self.spoolman.update_spool(spool["id"], {"location": location})
            placed.add(loaded.uuid)
        elif loaded.uuid not in self._warned_moved:
            self._warned_moved.add(loaded.uuid)
            log.warning(
                "Spool %s moved to %r after the bridge set it to %r. Leaving it there. If this was not you, check that only one bridge is running.",
                spool["id"], spool.get("location"), location,
            )

    def _previous_consumed(self, uuid: str, spool: dict) -> float | None:
        if self.state.seen_in_dry_run(uuid):
            return self.state.previous(uuid)
        stored = stored_consumed(spool)
        return stored if stored is not None else self.state.previous(uuid)

    def _sync_consumption(self, loaded: LoadedSpool, spool: dict) -> None:
        spool_id = spool["id"]
        if loaded.consumed_weight is None:
            log.warning("Connect reported no consumed weight for instance %s", loaded.uuid)
            return
        current = float(loaded.consumed_weight)
        previous = self._previous_consumed(loaded.uuid, spool)
        action, delta = consumption_step(previous, current)
        if action == "use" and implausible_use(loaded, delta):
            log.error(
                "Instance %s jumped from %s g to %s g consumed, more than a whole spool. Not deducting it; starting again from %s g",
                loaded.uuid, previous, current, current,
            )
            action = "rebase"
        if action == "baseline":
            log.info("First sighting of instance %s; baseline %s g, nothing deducted", loaded.uuid, current)
        elif action == "rebase":
            log.warning("Consumed weight for instance %s went from %s g to %s g; rebasing", loaded.uuid, previous, current)
        elif action == "use":
            log.info("Instance %s consumed %s g more", loaded.uuid, round(delta, 3))
        elif stored_consumed(spool) is None and not self.spoolman.dry_run:
            # Spools linked before the marker existed get it on their next poll.
            action = "baseline"
        else:
            return
        if action == "use":
            self._deduct(spool_id, delta, current)
        else:
            self.spoolman.update_spool(spool_id, {"extra": {**(spool.get("extra") or {}), CONSUMED_FIELD: encode_consumed(current)}})
        self.state.record(loaded.uuid, current, "use" if action == "use" else action)

    def _deduct(self, spool_id: int, delta: float, consumed: float) -> None:
        """Deduct and move the marker in one write, so a retry or a second bridge cannot deduct the same grams again."""
        fresh = self.spoolman.get_spool(spool_id)
        stored = stored_consumed(fresh)
        if stored is not None and stored >= consumed - WEIGHT_EPSILON:
            log.info("Spool %s already accounts for %s g consumed; something else deducted it", spool_id, stored)
            return
        if stored is not None:
            # The marker moved since the spool list was read: only deduct the rest.
            delta = consumed - stored
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        body = {
            "used_weight": round(float(fresh.get("used_weight") or 0) + delta, 3),
            "last_used": now,
            "extra": {**(fresh.get("extra") or {}), CONSUMED_FIELD: encode_consumed(consumed)},
        }
        if not fresh.get("first_used"):
            body["first_used"] = now
        self.spoolman.update_spool(spool_id, body)

    def link_spool(self, loaded: LoadedSpool, spool_id: int, selected: set[str], loaded_now: set[str] | None = None) -> None:
        """Apply the review page's choices. Other extra fields, like card_uids, are kept."""
        self._ensure_extra_fields()
        spool = self.spoolman.get_spool(spool_id)
        taken = extra_value(spool, SPOOL_UUID_FIELD)
        if taken and taken != loaded.uuid and not can_replace_link(loaded, taken, loaded_now or set()):
            raise SpoolmanError(f"Spool {spool_id} is already linked to instance {taken}")
        changes = {c["key"]: c for c in build_link_changes(loaded, spool, 0)}
        spool_extra = dict(spool.get("extra") or {})
        spool_extra[SPOOL_UUID_FIELD] = encode_extra(loaded.uuid)
        if loaded.consumed_weight is not None:
            spool_extra[CONSUMED_FIELD] = encode_consumed(loaded.consumed_weight)
        spool_body: dict = {"extra": spool_extra}
        filament_body: dict = {}
        filament = spool.get("filament") or {}
        for key in selected:
            change = changes.get(key)
            if not change or change["locked"]:
                continue
            if change["field"] == FILAMENT_UUID_FIELD:
                extra = dict(filament.get("extra") or {})
                extra[FILAMENT_UUID_FIELD] = encode_extra(change["after"])
                filament_body["extra"] = extra
            elif change["target"] == "spool":
                spool_body[change["field"]] = change["after"]
            else:
                filament_body[change["field"]] = change["after"]
        if filament_body:
            self.spoolman.update_filament(filament["id"], dict(sorted(filament_body.items())))
        self.spoolman.update_spool(spool_id, dict(sorted(spool_body.items())))
        # The spool now accounts for everything up to this point, or starts counting from it.
        if loaded.consumed_weight is not None and not self.spoolman.dry_run:
            self.state.record(loaded.uuid, float(loaded.consumed_weight), "baseline")

    def unlink_spool(self, spool_id: int) -> None:
        spool = self.spoolman.get_spool(spool_id)
        kept = {k: v for k, v in (spool.get("extra") or {}).items() if k not in (SPOOL_UUID_FIELD, CONSUMED_FIELD)}
        try:
            # Spoolman 0.27 merges extra fields per key, so a field is only removed when it is sent as null.
            self.spoolman.update_spool(spool_id, {"extra": {**kept, SPOOL_UUID_FIELD: None, CONSUMED_FIELD: None}})
        except SpoolmanError as e:
            if "HTTP 422" not in str(e):
                raise
            # Older Spoolman replaces the whole extra dict and does not accept null values.
            self.spoolman.update_spool(spool_id, {"extra": kept})
        if not self.spoolman.dry_run and extra_value(self.spoolman.get_spool(spool_id), SPOOL_UUID_FIELD):
            raise SpoolmanError(f"Spoolman kept the link on spool {spool_id}")

    def _existing_filament(self, loaded: LoadedSpool) -> dict | None:
        """The same OpenPrintTag material first, then the same brand and name."""
        filaments = self.spoolman.list_filaments()
        if loaded.material_uuid:
            for filament in filaments:
                if extra_value(filament, FILAMENT_UUID_FIELD) == loaded.material_uuid:
                    return filament
        vendor_id = self._existing_vendor_id(loaded)
        name = (loaded.material_name or "").casefold()
        for filament in filaments:
            if vendor_id is not None and (filament.get("vendor") or {}).get("id") == vendor_id and (filament.get("name") or "").casefold() == name:
                return filament
        return None

    def _existing_vendor_id(self, loaded: LoadedSpool) -> int | None:
        brand = (loaded.brand or "").casefold()
        return next((v["id"] for v in self.spoolman.list_vendors() if v["name"].casefold() == brand), None)

    def create_preview(self, loaded: LoadedSpool) -> dict:
        filament_exists = self._existing_filament(loaded) is not None
        return {"vendor_exists": filament_exists or self._existing_vendor_id(loaded) is not None, "filament_exists": filament_exists}

    def create_spool(self, loaded: LoadedSpool, location: str) -> dict | None:
        self._ensure_extra_fields()
        problems = [
            name
            for name, value in (
                ("brand", loaded.brand),
                ("material name", loaded.material_name),
                ("density", loaded.density),
                ("filament diameter", loaded.filament_diameter),
            )
            if value is None
        ]
        if problems:
            raise SpoolmanError(f"Cannot create a spool for instance {loaded.uuid}; Connect did not report: {', '.join(problems)}")
        filament_id = self._find_or_create_filament(loaded)
        consumed = float(loaded.consumed_weight or 0)
        created = self.spoolman.create_spool(
            {
                "filament_id": filament_id,
                "initial_weight": loaded.full_weight,
                "spool_weight": loaded.empty_weight,
                # Filament used before the spool reached Spoolman, so remaining weight is right.
                "used_weight": consumed,
                "location": location or None,
                "extra": {SPOOL_UUID_FIELD: encode_extra(loaded.uuid), CONSUMED_FIELD: encode_consumed(consumed)},
            }
        )
        if created is not None:
            log.info("Created Spoolman spool %s for instance %s", created["id"], loaded.uuid)
        if loaded.consumed_weight is not None:
            self.state.record(loaded.uuid, consumed, "baseline")
        return created

    def _find_or_create_vendor(self, loaded: LoadedSpool) -> int | str:
        vendor_id = self._existing_vendor_id(loaded)
        if vendor_id is not None:
            return vendor_id
        created = self.spoolman.create_vendor({"name": loaded.brand})
        return created["id"] if created else "<id of new vendor>"

    def _find_or_create_filament(self, loaded: LoadedSpool) -> int | str:
        existing = self._existing_filament(loaded)
        if existing:
            log.info("Using existing Spoolman filament %s", existing["id"])
            return existing["id"]
        vendor_id = self._find_or_create_vendor(loaded)
        body = {
            "vendor_id": vendor_id,
            "name": loaded.material_name,
            "material": loaded.material_type,
            "density": loaded.density,
            "diameter": loaded.filament_diameter,
            "weight": loaded.nominal_weight,
            "spool_weight": loaded.empty_weight,
            "color_hex": loaded.color_hex,
        }
        if loaded.material_uuid:
            body["extra"] = {FILAMENT_UUID_FIELD: encode_extra(loaded.material_uuid)}
        created = self.spoolman.create_filament(body)
        return created["id"] if created else "<id of new filament>"
