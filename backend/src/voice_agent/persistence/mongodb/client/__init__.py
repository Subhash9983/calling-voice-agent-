"""Long-lived PyMongo Async client lifecycle (docs/02 §21; docs/03 §18; docs/13 §9).

One ``AsyncMongoClient`` per process/event loop, created inside the running
loop, with explicit bounded timeouts, TLS through the Atlas SRV connection,
majority writes, and tz-aware UTC codecs. Only the approved database is ever
opened; no other database is listed or read. The URI is read from a
``SecretStr`` at open time and never logged, stored, or echoed.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from pydantic import SecretStr
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.server_api import ServerApi

from voice_agent.persistence.mongodb.client.errors import translate_errors
from voice_agent.persistence.mongodb.codecs.bson_codec import CODEC_OPTIONS
from voice_agent.ports.control_plane import StoreUnavailableError
from voice_agent.security.settings import APPROVED_DATABASE

DEFAULT_PING_TIMEOUT_S: Final = 2.0
ClientFactory = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class MongoClientSettings:
    """Bounded driver timeouts (docs/04 §20: API database timeouts are bounded).

    Request handlers additionally bound every store call with the 2 s API
    dependency timeout; these driver limits cap background/maintenance work.
    """

    app_name: str = "voice-agent"
    server_selection_timeout_ms: int = 5_000
    connect_timeout_ms: int = 5_000
    operation_timeout_ms: int = 10_000
    max_pool_size: int = 10
    max_idle_time_ms: int = 60_000

    def client_options(self) -> dict[str, Any]:
        return {
            "appname": self.app_name,
            "serverSelectionTimeoutMS": self.server_selection_timeout_ms,
            "connectTimeoutMS": self.connect_timeout_ms,
            "timeoutMS": self.operation_timeout_ms,
            "maxPoolSize": self.max_pool_size,
            "minPoolSize": 0,
            "maxIdleTimeMS": self.max_idle_time_ms,
            "retryWrites": True,
            "retryReads": True,
            "w": "majority",
            "tz_aware": True,
            "tzinfo": CODEC_OPTIONS.tzinfo,
            "server_api": ServerApi("1"),
            "connect": False,
        }


class MongoPersistence:
    """Owns the client and the single approved database handle."""

    def __init__(
        self,
        uri: SecretStr,
        *,
        database_name: str = APPROVED_DATABASE,
        settings: MongoClientSettings | None = None,
        client_factory: ClientFactory = AsyncMongoClient,
    ) -> None:
        if database_name != APPROVED_DATABASE:
            raise ValueError("only the approved R&D database may be opened")
        self._uri = uri
        self._database_name = database_name
        self._settings = settings or MongoClientSettings()
        self._factory = client_factory
        self._client: Any | None = None
        self._database: Any | None = None

    @classmethod
    def from_handles(cls, client: Any, database: Any) -> MongoPersistence:
        """Wrap already-open handles (tests and fakes)."""
        instance = cls(SecretStr(""), client_factory=lambda *_a, **_k: client)
        instance._client = client
        instance._database = database
        return instance

    @property
    def is_open(self) -> bool:
        return self._database is not None

    @property
    def database_name(self) -> str:
        return self._database_name

    def open(self) -> None:
        """Create the client inside the running loop; no network I/O happens here."""
        if self._client is not None:
            return
        asyncio.get_running_loop()
        options = self._settings.client_options()
        self._client = self._factory(self._uri.get_secret_value(), **options)
        self._database = self._client.get_database(self._database_name)

    @property
    def client(self) -> Any:
        if self._client is None:
            raise StoreUnavailableError("database client is not open")
        return self._client

    @property
    def database(self) -> AsyncDatabase[dict[str, Any]]:
        if self._database is None:
            raise StoreUnavailableError("database client is not open")
        database: AsyncDatabase[dict[str, Any]] = self._database
        return database

    async def ping(self, *, timeout_s: float = DEFAULT_PING_TIMEOUT_S) -> None:
        """Bounded readiness probe; raises ``StoreUnavailableError`` on any failure."""
        database = self.database
        try:
            async with asyncio.timeout(timeout_s):
                async with translate_errors():
                    await database.command("ping")
        except TimeoutError:
            raise StoreUnavailableError("database ping timed out") from None

    async def close(self) -> None:
        client, self._client, self._database = self._client, None, None
        if client is not None:
            await client.aclose()
