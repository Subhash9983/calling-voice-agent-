"""LiveKit control-plane adapter over ``livekit-api`` (docs/06 §3-§6, §17, §19).

- ``prepare_session`` creates an opaque ``va-rd-<uuid>`` room bounded to two
  participants with short empty/departure timeouts (a cost backstop if
  cleanup fails), then one explicit named-agent dispatch whose metadata is
  only the allowlisted locator; a dispatch failure deletes the room;
- ``issue_join_token`` signs a 10-minute token for exactly one room and
  opaque browser identity with join, microphone-only publish, subscribe and
  data grants and no admin/record/metadata grants;
- ``inspect_session``/``release_session``/``notify_end_requested`` read safe
  status, clean up idempotently, and send the targeted reliable
  ``va.control.v1`` wake-up.

Server credentials stay in ``SecretStr`` and are never repr'd. Every
provider failure becomes :class:`TransportControlError` with a stable code
and no provider message, payload, or network address.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Final, Protocol

from livekit import api
from pydantic import SecretStr

from voice_agent.contracts.dispatch import DispatchLocator, encode_dispatch_metadata
from voice_agent.contracts.realtime_wire import (
    CONTROL_TOPIC,
    EndRequestedSignal,
    encode_end_requested,
)
from voice_agent.ports.transport_control import (
    JOIN_TOKEN_LIFETIME_S,
    JoinCredential,
    TransportAllocation,
    TransportControlError,
    TransportStatus,
)

LIVEKIT_PROVIDER: Final = "livekit"
CONTROL_ADAPTER_VERSION: Final = "livekit-control-0.1.0"
ROOM_PREFIX: Final = "va-rd-"
BROWSER_PREFIX: Final = "va-user-"
AGENT_PREFIX: Final = "va-agent-"
MAX_ROOM_PARTICIPANTS: Final = 2
# Room closes if nobody joins within this bound, and shortly after the last
# participant leaves (the 20 s reconnect window); a cleanup backstop.
ROOM_EMPTY_TIMEOUT_S: Final = 60
ROOM_DEPARTURE_TIMEOUT_S: Final = 20
MICROPHONE_SOURCE: Final = "microphone"


class _RoomService(Protocol):
    async def create_room(self, create: api.CreateRoomRequest, /) -> api.Room: ...

    async def list_rooms(self, request: api.ListRoomsRequest, /) -> api.ListRoomsResponse: ...

    async def list_participants(
        self, request: api.ListParticipantsRequest, /
    ) -> api.ListParticipantsResponse: ...

    async def delete_room(self, delete: api.DeleteRoomRequest, /) -> api.DeleteRoomResponse: ...

    async def send_data(self, send: api.SendDataRequest, /) -> api.SendDataResponse: ...


class _DispatchService(Protocol):
    async def create_dispatch(
        self, req: api.CreateAgentDispatchRequest, /
    ) -> api.AgentDispatch: ...

    async def delete_dispatch(self, dispatch_id: str, room_name: str, /) -> api.AgentDispatch: ...

    async def get_dispatch(
        self, dispatch_id: str, room_name: str, /
    ) -> api.AgentDispatch | None: ...


class LiveKitServerApi(Protocol):
    """The subset of ``livekit.api.LiveKitAPI`` this adapter uses."""

    @property
    def room(self) -> _RoomService: ...

    @property
    def agent_dispatch(self) -> _DispatchService: ...

    async def aclose(self) -> None: ...


ApiFactory = Callable[[], LiveKitServerApi]

_AUTH_CODES: Final = frozenset({"unauthenticated", "permission_denied"})
_NOT_FOUND: Final = "not_found"


def _normalized(error: BaseException, *, code: str) -> TransportControlError:
    if isinstance(error, api.ServerError):
        if error.code in _AUTH_CODES:
            return TransportControlError(
                "transport authentication failed", code="authentication_failed", retryable=False
            )
        if error.code == "resource_exhausted":
            return TransportControlError("transport rate-limited", code="rate_limited")
        if error.code == _NOT_FOUND:
            return TransportControlError("transport target not found", code="room_not_found")
        if error.code == "unavailable":
            return TransportControlError("transport unavailable", code="transport_unavailable")
        return TransportControlError(f"transport {code.replace('_', ' ')}", code=code)
    return TransportControlError("transport unavailable", code="transport_unavailable")


# Any provider/client failure (server error, HTTP client error, timeout,
# network error) is normalized; nothing provider-specific escapes.
_PROVIDER_ERRORS: Final = (Exception,)


async def _call[T](awaitable: Awaitable[T], *, code: str) -> T:
    """Await a provider call, normalizing failures outside the ``except`` block."""
    failure: BaseException | None = None
    try:
        return await awaitable
    except _PROVIDER_ERRORS as exc:
        failure = exc
    raise _normalized(failure, code=code) from None


def _is_not_found(error: BaseException) -> bool:
    return isinstance(error, api.ServerError) and error.code == _NOT_FOUND


class LiveKitTransportControl:
    def __init__(
        self,
        *,
        url: str,
        api_key: SecretStr,
        api_secret: SecretStr,
        api_factory: ApiFactory | None = None,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._api_secret = api_secret
        self._factory = api_factory or self._default_factory
        self._api: LiveKitServerApi | None = None

    def __repr__(self) -> str:
        return f"{type(self).__name__}(provider={LIVEKIT_PROVIDER!r})"

    @property
    def provider(self) -> str:
        return LIVEKIT_PROVIDER

    @property
    def is_available(self) -> bool:
        return True

    @property
    def public_url(self) -> str:
        return self._url

    def _default_factory(self) -> LiveKitServerApi:
        # Callers bound every control call (the control API's dependency
        # timeout); the SDK's own 10 s client timeout is the outer backstop.
        return api.LiveKitAPI(
            url=self._url,
            api_key=self._api_key.get_secret_value(),
            api_secret=self._api_secret.get_secret_value(),
        )

    def _client(self) -> LiveKitServerApi:
        # Created lazily inside the running loop (aiohttp sessions are loop-bound).
        if self._api is None:
            self._api = self._factory()
        return self._api

    async def prepare_session(
        self, locator: DispatchLocator, *, agent_name: str
    ) -> TransportAllocation:
        client = self._client()
        room_name = f"{ROOM_PREFIX}{uuid.uuid4()}"
        room = api.CreateRoomRequest(
            name=room_name,
            empty_timeout=ROOM_EMPTY_TIMEOUT_S,
            departure_timeout=ROOM_DEPARTURE_TIMEOUT_S,
            max_participants=MAX_ROOM_PARTICIPANTS,
        )
        await _call(client.room.create_room(room), code="room_creation_failed")
        request = api.CreateAgentDispatchRequest(
            agent_name=agent_name, room=room_name, metadata=encode_dispatch_metadata(locator)
        )
        try:
            dispatch = await _call(
                client.agent_dispatch.create_dispatch(request), code="dispatch_failed"
            )
        except TransportControlError:
            await self._delete_room(room_name)
            raise
        return TransportAllocation(
            provider=LIVEKIT_PROVIDER,
            room_name=room_name,
            participant_identity=f"{BROWSER_PREFIX}{uuid.uuid4().hex}",
            dispatch_id=dispatch.id or None,
            agent_identity=f"{AGENT_PREFIX}{uuid.uuid4().hex}",
        )

    async def issue_join_token(
        self, allocation: TransportAllocation, *, now: datetime
    ) -> JoinCredential:
        if allocation.provider != LIVEKIT_PROVIDER:
            raise TransportControlError("allocation belongs to another transport", code="invalid")
        lifetime = timedelta(seconds=JOIN_TOKEN_LIFETIME_S)
        grants = api.VideoGrants(
            room_join=True,
            room=allocation.room_name,
            can_publish=True,
            can_publish_sources=[MICROPHONE_SOURCE],
            can_subscribe=True,
            can_publish_data=True,
            can_update_own_metadata=False,
        )
        token = (
            api.AccessToken(self._api_key.get_secret_value(), self._api_secret.get_secret_value())
            .with_identity(allocation.participant_identity)
            .with_ttl(lifetime)
            .with_grants(grants)
        )
        return JoinCredential(token=token.to_jwt(), expires_at=now + lifetime)

    async def inspect_session(self, allocation: TransportAllocation) -> TransportStatus:
        client = self._client()
        rooms = await _call(
            client.room.list_rooms(api.ListRoomsRequest(names=[allocation.room_name])),
            code="inspection_failed",
        )
        if not any(room.name == allocation.room_name for room in rooms.rooms):
            return TransportStatus(
                room_exists=False, browser_present=False, agent_present=False, participant_count=0
            )
        try:
            listing = await _call(
                client.room.list_participants(
                    api.ListParticipantsRequest(room=allocation.room_name)
                ),
                code="inspection_failed",
            )
        except TransportControlError as failure:
            if failure.code != "room_not_found":
                raise
            return TransportStatus(
                room_exists=False, browser_present=False, agent_present=False, participant_count=0
            )
        identities = {participant.identity for participant in listing.participants}
        return TransportStatus(
            room_exists=True,
            browser_present=allocation.participant_identity in identities,
            agent_present=allocation.agent_identity in identities,
            participant_count=len(identities),
            dispatch_present=await self._dispatch_present(allocation),
        )

    async def _dispatch_present(self, allocation: TransportAllocation) -> bool | None:
        if allocation.dispatch_id is None:
            return None
        try:
            found = await _call(
                self._client().agent_dispatch.get_dispatch(
                    allocation.dispatch_id, allocation.room_name
                ),
                code="inspection_failed",
            )
        except TransportControlError as failure:
            if failure.code != "room_not_found":
                raise
            return False
        return found is not None

    async def notify_end_requested(
        self, allocation: TransportAllocation, signal: EndRequestedSignal
    ) -> None:
        if allocation.agent_identity is None:
            raise TransportControlError("no agent identity to notify", code="invalid")
        request = api.SendDataRequest(
            room=allocation.room_name,
            data=encode_end_requested(signal),
            kind=api.DataPacket.Kind.RELIABLE,
            destination_identities=[allocation.agent_identity],
            topic=CONTROL_TOPIC,
        )
        await _call(self._client().room.send_data(request), code="data_send_failed")

    async def release_session(self, allocation: TransportAllocation) -> None:
        """Delete the dispatch then the room; every step runs even if one fails."""
        failed = False
        if allocation.dispatch_id is not None:
            failed = not await self._delete_dispatch(allocation)
        failed = not await self._delete_room(allocation.room_name) or failed
        if failed:
            raise TransportControlError("transport cleanup failed", code="cleanup_failed")

    async def _delete_dispatch(self, allocation: TransportAllocation) -> bool:
        dispatch_id = allocation.dispatch_id or ""
        call = self._client().agent_dispatch.delete_dispatch(dispatch_id, allocation.room_name)
        return await _tolerant(call)

    async def _delete_room(self, room_name: str) -> bool:
        call = self._client().room.delete_room(api.DeleteRoomRequest(room=room_name))
        return await _tolerant(call)

    async def aclose(self) -> None:
        client, self._api = self._api, None
        if client is not None:
            await _tolerant(client.aclose())


async def _tolerant(awaitable: Awaitable[object]) -> bool:
    """``True`` when the call succeeded or its target is already gone (idempotent)."""
    try:
        await awaitable
    except _PROVIDER_ERRORS as exc:
        return _is_not_found(exc)
    return True
