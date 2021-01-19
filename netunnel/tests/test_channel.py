from netunnel.common import channel

import contextlib
import asyncio
import aiohttp
import pytest


def test_messages_encoding():
    data = {
        'test_message': channel.Messages.TUNNEL_SERVER_TO_CLIENT_NEW_CONNECTION
    }
    assert channel.message_bson_loader(channel.message_bson_dumps(data)) == data


def test_api_message():
    message = channel.ChannelMessage(message_type=channel.Messages.TUNNEL_SERVER_TO_CLIENT_NEW_CONNECTION)
    with pytest.raises(channel.ChannelError):
        message.get_valid_response()
    with pytest.raises(channel.ChannelError):
        message.get_error_response('')
    message = channel.ChannelMessage(message_type=channel.Messages.TUNNEL_SERVER_TO_CLIENT_NEW_CONNECTION,
                                     _identifier=0)
    assert isinstance(message.get_valid_response(), channel.ChannelResponse)
    assert isinstance(message.get_error_response(''), channel.ChannelResponse)


def test_api_response():
    message = channel.ChannelMessage(message_type=channel.Messages.TUNNEL_SERVER_TO_CLIENT_NEW_CONNECTION,
                                     _identifier=0)
    assert message.get_valid_response().is_ok()
    error_response = message.get_error_response('Error')
    assert not error_response.is_ok() and error_response.error == 'Error'


class MockWSMsg:
    data = None

class MockWebsocket:
    def __init__(self):
        self.closed = False
        self._linked_mock_websocket: MockWebsocket = None
        self._data_to_retrieve = asyncio.Queue()

    async def add_data(self, data):
        await self._data_to_retrieve.put(data)

    async def send_bytes(self, data: bytes):
        await self._linked_mock_websocket.add_data(data)

    def link_mock_websocket(self, mock_websocket):
        self._linked_mock_websocket = mock_websocket

    def __aiter__(self):
        return self

    async def __anext__(self):
        msg = MockWSMsg()
        msg.type = aiohttp.WSMsgType.BINARY
        msg.data = await self._data_to_retrieve.get()
        return msg

    async def close(self):
        self.closed = True


async def test_channel():
    async def echo_handler(msg: channel.ChannelMessage) -> channel.ChannelResponse:
        return msg.get_valid_response(msg.data)

    server_websocket = MockWebsocket()
    client_websocket = MockWebsocket()
    server_websocket.link_mock_websocket(client_websocket)
    client_websocket.link_mock_websocket(server_websocket)
    channel_server = channel.Channel(websocket=server_websocket, channel_id=0, handler=echo_handler)
    channel_client = channel.Channel(websocket=client_websocket, channel_id=0)
    channel_server_task = asyncio.ensure_future(channel_server.serve())
    channel_client_task = asyncio.ensure_future(channel_client.serve())
    try:
        data_payload = {'id': channel_client.id}
        message = channel.ChannelMessage(channel.Messages.TUNNEL_SERVER_TO_CLIENT_NEW_CONNECTION, data=data_payload)
        try:
            response = await asyncio.wait_for(channel_client.send_message(message), timeout=2)
        except asyncio.TimeoutError:
            pytest.fail('No response for Channel.send_message after 2 seconds')
        assert response.data == data_payload
    finally:
        for c in (channel_client_task, channel_server_task):
            c.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await c