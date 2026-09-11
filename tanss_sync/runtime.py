"""Verdrahtung: aus einer Konfiguration die einsatzbereiten Bausteine bauen."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass

from .config.models import AppConfig
from .config.secrets import SecretRef
from .config.store import ConfigStore
from .microsoft.auth import GraphAuth
from .microsoft.client import GraphClient
from .microsoft.repository import GraphRepository
from .state.store import StateStore
from .tanss.auth import TanssAuth
from .tanss.client import TanssClient
from .tanss.repository import TanssRepository
from .util.lock import ProcessLock

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Runtime:
    config: AppConfig
    store: ConfigStore
    tanss: TanssRepository
    auth: TanssAuth
    state: StateStore
    graph: GraphRepository | None = None

    def close(self) -> None:
        self.tanss.client.close()
        if self.graph is not None:
            self.graph.client.close()
        self.state.close()


def build_tanss(config: AppConfig) -> tuple[TanssRepository, TanssAuth]:
    auth = TanssAuth(
        SecretRef(config.tanss.token_ref),
        owner_employee_id=config.tanss.token_owner_employee_id,
        duration_days=config.tanss.token_duration_days,
    )
    client = TanssClient(
        config.tanss.api_base, auth,
        timeout=config.tanss.timeout_seconds,
        verify_tls=config.tanss.verify_tls,
        log_http=config.logging.log_http,
    )
    return TanssRepository(client, config.tanss.own_company_id), auth


def build_graph(config: AppConfig) -> GraphRepository:
    client = GraphClient(
        GraphAuth(config.microsoft),
        timeout=config.microsoft.timeout_seconds,
        log_http=config.logging.log_http,
    )
    return GraphRepository(client, timezone=config.microsoft.timezone)


@contextmanager
def open_runtime(config_path: str | None = None, *, with_graph: bool = True):
    """Baut alles auf und räumt zuverlässig wieder ab."""
    store = ConfigStore.discover(config_path)
    config = store.load()
    _configure_logging(config)

    tanss, auth = build_tanss(config)
    state = StateStore(config.state.db_path)
    state.migrate()

    graph = None
    if with_graph:
        try:
            graph = build_graph(config)
        except Exception as exc:  # noqa: BLE001 - ohne Graph geht vieles trotzdem
            log.warning("Microsoft-Zugang nicht nutzbar: %s", exc)

    runtime = Runtime(config, store, tanss, auth, state, graph)
    try:
        yield runtime
    finally:
        runtime.close()


@contextmanager
def write_lock(config: AppConfig, command: str):
    """Schreibende Befehle laufen nie parallel."""
    with ProcessLock(config.state.lock_path, command):
        yield


def _configure_logging(config: AppConfig) -> None:
    level = getattr(logging, config.logging.level.upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if config.logging.file:
        from logging.handlers import RotatingFileHandler
        from pathlib import Path

        path = Path(config.logging.file).expanduser()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(RotatingFileHandler(
                path, maxBytes=config.logging.rotate_mb * 1024 * 1024,
                backupCount=config.logging.keep_files, encoding="utf-8"))
        except OSError as exc:
            log.warning("Protokolldatei %s nicht beschreibbar: %s", path, exc)

    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s — %(message)s",
        handlers=handlers,
        force=True,
    )
