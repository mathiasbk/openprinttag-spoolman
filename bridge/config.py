"""Settings from DATA_DIR/config.json, with the environment and .env as defaults."""

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_ACCOUNT_URL = "https://account.prusa3d.com"
# The Connect web app's public OAuth client id, from connect.prusa3d.com/environment.js.
DEFAULT_ACCOUNT_CLIENT_ID = "MRHTlZhZqkNrrQ6FUPtjyusAz8nc59ErHXP8XkS4"
DEFAULT_GRAPHQL_URL = "https://connect-api.prusa3d.com/graphql"
DEFAULT_WEB_PORT = 7913


class ConfigError(Exception):
    pass


def load_dotenv(path: Path = Path(".env")) -> None:
    """Load KEY=VALUE lines. Variables already in the environment win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _flag(raw, name: str, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if raw is None or raw == "":
        return default
    if raw.lower() in ("1", "true", "yes", "on"):
        return True
    if raw.lower() in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} must be true or false, got {raw!r}")


@dataclass(frozen=True)
class PrinterConfig:
    uuid: str
    organization_id: str
    name: str = ""
    location: str = ""
    enabled: bool = True


@dataclass(frozen=True)
class Settings:
    spoolman_url: str = ""
    spoolman_ca_bundle: str | None = None
    spoolman_verify_tls: bool = True
    poll_interval: int = 60
    warehouse_interval: int = 600
    dry_run: bool = True
    auto_create: bool = False
    track_usage: bool = True
    printers: tuple[PrinterConfig, ...] = ()
    data_dir: Path = Path("/data")
    web_port: int = DEFAULT_WEB_PORT
    account_url: str = DEFAULT_ACCOUNT_URL
    account_client_id: str = DEFAULT_ACCOUNT_CLIENT_ID
    graphql_url: str = DEFAULT_GRAPHQL_URL

    @property
    def tokens_path(self) -> Path:
        return self.data_dir / "tokens.json"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def enabled_printers(self) -> tuple[PrinterConfig, ...]:
        return tuple(p for p in self.printers if p.enabled)


def config_path(data_dir: Path) -> Path:
    return data_dir / "config.json"


def read_config(data_dir: Path) -> dict:
    try:
        return json.loads(config_path(data_dir).read_text())
    except FileNotFoundError:
        return {}
    except ValueError as e:
        raise ConfigError(f"{config_path(data_dir)} is not valid JSON") from e


def update_config(data_dir: Path, changes: dict) -> None:
    config = read_config(data_dir)
    config.update(changes)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = config_path(data_dir)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _env_printers(env: dict) -> tuple[PrinterConfig, ...]:
    if not (env.get("CONNECT_PRINTER_UUID") and env.get("CONNECT_ORGANIZATION_ID")):
        return ()
    return (
        PrinterConfig(
            uuid=env["CONNECT_PRINTER_UUID"],
            organization_id=env["CONNECT_ORGANIZATION_ID"],
            location=env.get("LOCATION_NAME") or "Core One",
        ),
    )


def _config_printers(raw: list) -> tuple[PrinterConfig, ...]:
    try:
        return tuple(
            PrinterConfig(
                uuid=p["uuid"],
                organization_id=p["organization_id"],
                name=p.get("name") or "",
                location=p.get("location") or p.get("name") or "",
                enabled=bool(p.get("enabled", True)),
            )
            for p in raw
        )
    except (KeyError, TypeError) as e:
        raise ConfigError("The printers list in config.json is invalid") from e


def load_settings(env: dict | None = None) -> Settings:
    """A value in config.json wins, because that is what the web interface saves."""
    env = dict(os.environ) if env is None else env
    data_dir = Path(env.get("DATA_DIR") or "/data")
    config = read_config(data_dir)

    def value(config_key: str, env_key: str):
        return config[config_key] if config_key in config else env.get(env_key)

    try:
        poll_interval = int(value("poll_interval", "POLL_INTERVAL") or 60)
        warehouse_interval = int(value("warehouse_interval", "WAREHOUSE_INTERVAL") or 600)
        web_port = int(env.get("WEB_PORT") or DEFAULT_WEB_PORT)
    except (TypeError, ValueError) as e:
        raise ConfigError("POLL_INTERVAL, WAREHOUSE_INTERVAL and WEB_PORT must be whole numbers") from e
    if poll_interval < 10:
        raise ConfigError("POLL_INTERVAL must be at least 10 seconds")
    if warehouse_interval < 60:
        raise ConfigError("WAREHOUSE_INTERVAL must be at least 60 seconds")
    return Settings(
        spoolman_url=(value("spoolman_url", "SPOOLMAN_URL") or "").rstrip("/"),
        spoolman_ca_bundle=value("spoolman_ca_bundle", "SPOOLMAN_CA_BUNDLE") or None,
        spoolman_verify_tls=_flag(value("spoolman_verify_tls", "SPOOLMAN_VERIFY_TLS"), "SPOOLMAN_VERIFY_TLS", True),
        poll_interval=poll_interval,
        warehouse_interval=warehouse_interval,
        dry_run=_flag(value("dry_run", "DRY_RUN"), "DRY_RUN", True),
        auto_create=_flag(value("auto_create", "AUTO_CREATE"), "AUTO_CREATE", False),
        track_usage=_flag(value("track_usage", "TRACK_USAGE"), "TRACK_USAGE", True),
        printers=_config_printers(config["printers"]) if "printers" in config else _env_printers(env),
        data_dir=data_dir,
        web_port=web_port,
        account_url=(env.get("CONNECT_ACCOUNT_URL") or DEFAULT_ACCOUNT_URL).rstrip("/"),
        account_client_id=env.get("CONNECT_ACCOUNT_CLIENT_ID") or DEFAULT_ACCOUNT_CLIENT_ID,
        graphql_url=env.get("CONNECT_GRAPHQL_URL") or DEFAULT_GRAPHQL_URL,
    )
