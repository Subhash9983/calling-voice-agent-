"""Fake of the ``livekit-api`` server client subset used by the control adapter.

Records rooms, dispatches, deletions, and sent data; failures can be injected
per operation. Uses real ``livekit.protocol`` messages, no network.
"""

from __future__ import annotations

from livekit import api

TEST_KEY = "APItestkey000"
TEST_SECRET = "test-secret-0123456789-0123456789-abcdef"  # noqa: S105 - throwaway test secret


class FakeRooms:
    def __init__(self, api_state: FakeApi) -> None:
        self._state = api_state

    async def create_room(self, create: api.CreateRoomRequest, /) -> api.Room:
        self._state.check("create_room")
        self._state.rooms[create.name] = create
        return api.Room(name=create.name, sid="RM_1")

    async def list_rooms(self, request: api.ListRoomsRequest, /) -> api.ListRoomsResponse:
        self._state.check("list_rooms")
        names = [n for n in request.names if n in self._state.rooms]
        return api.ListRoomsResponse(rooms=[api.Room(name=n) for n in names])

    async def list_participants(
        self, request: api.ListParticipantsRequest, /
    ) -> api.ListParticipantsResponse:
        self._state.check("list_participants")
        people = self._state.participants.get(request.room, [])
        return api.ListParticipantsResponse(
            participants=[api.ParticipantInfo(identity=p) for p in people]
        )

    async def delete_room(self, delete: api.DeleteRoomRequest, /) -> api.DeleteRoomResponse:
        self._state.check("delete_room")
        if delete.room not in self._state.rooms:
            raise api.ServerError("not_found", "room not found", status=404)
        del self._state.rooms[delete.room]
        return api.DeleteRoomResponse()

    async def send_data(self, send: api.SendDataRequest, /) -> api.SendDataResponse:
        self._state.check("send_data")
        self._state.sent.append(send)
        return api.SendDataResponse()


class FakeDispatches:
    def __init__(self, api_state: FakeApi) -> None:
        self._state = api_state

    async def create_dispatch(self, req: api.CreateAgentDispatchRequest, /) -> api.AgentDispatch:
        self._state.check("create_dispatch")
        self._state.dispatches.append(req)
        return api.AgentDispatch(id=f"AD_{len(self._state.dispatches)}", room=req.room)

    async def delete_dispatch(self, dispatch_id: str, room_name: str, /) -> api.AgentDispatch:
        self._state.check("delete_dispatch")
        self._state.deleted_dispatches.append(dispatch_id)
        return api.AgentDispatch(id=dispatch_id)

    async def get_dispatch(self, dispatch_id: str, room_name: str, /) -> api.AgentDispatch | None:
        self._state.check("get_dispatch")
        if dispatch_id in self._state.deleted_dispatches:
            return None
        return api.AgentDispatch(id=dispatch_id)


class FakeApi:
    def __init__(self, failures: dict[str, Exception] | None = None) -> None:
        self.failures = failures or {}
        self.rooms: dict[str, api.CreateRoomRequest] = {}
        self.participants: dict[str, list[str]] = {}
        self.dispatches: list[api.CreateAgentDispatchRequest] = []
        self.deleted_dispatches: list[str] = []
        self.sent: list[api.SendDataRequest] = []
        self.closed = 0
        self.room = FakeRooms(self)
        self.agent_dispatch = FakeDispatches(self)

    def check(self, operation: str) -> None:
        failure = self.failures.get(operation)
        if failure is not None:
            raise failure

    async def aclose(self) -> None:
        self.closed += 1
