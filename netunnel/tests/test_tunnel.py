import pytest
import asyncio
import aiohttp
import logging

from netunnel.common.tunnel import _ConnectionHandler


@pytest.fixture
async def echo_server_socket(loop, aiohttp_unused_port):
    """
    Creates a server which echo back whatever it receive.
    The value is the port of the server, while the address is 127.0.0.1
    """
    async def handler(reader, writer):
        data = await reader.read(1024)
        writer.write(data)
        await writer.drain()
        writer.close()
    port = aiohttp_unused_port()
    server = await asyncio.start_server(handler, host='127.0.0.1', port=port)
    yield '127.0.0.1', port
    server.close()
    await server.wait_closed()


class MockWebsocket:
    def __init__(self, items: list, hook_iteration=None):
        self._iter = iter(items)
        self._result = []
        self._hook_iteration = hook_iteration
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            res = aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, next(self._iter), None)
            if self._hook_iteration:
                await self._hook_iteration(res)
            return res
        except StopIteration:
            self.closed = True
            raise StopAsyncIteration

    async def send_bytes(self, data):
        self._result.append(data)

    def assert_received(self, expected):
        return expected == self._result


async def test_connection_handler(echo_server_socket, bytes_data):
    host, port = echo_server_socket

    reader, writer = await asyncio.open_connection(host=host, port=port)
    data_chunks = [bytes_data for i in range(5)]
    mock_websocket = MockWebsocket(data_chunks)
    conn = _ConnectionHandler(websocket=mock_websocket,
                              connection_writer=writer,
                              connection_reader=reader,
                              logger=logging.getLogger())
    try:
        await asyncio.wait_for(conn.run_until_eof(), timeout=5)
    except asyncio.TimeoutError:
        pytest.fail('_ConnectionHandler did not reach eof')
    mock_websocket.assert_received(b''.join(data_chunks))
    assert conn._websocket_to_connection_task.done()
    assert conn._connection_to_websocket_task.done()


async def test_connection_handler_health_check(echo_server_socket, bytes_data):
    conn: _ConnectionHandler = None
    async def assert_health_check(data):
        assert await conn.health_check()
    host, port = echo_server_socket

    reader, writer = await asyncio.open_connection(host=host, port=port)
    mock_websocket = MockWebsocket([bytes_data, bytes_data], hook_iteration=assert_health_check)
    conn = _ConnectionHandler(websocket=mock_websocket,
                              connection_writer=writer,
                              connection_reader=reader,
                              logger=logging.getLogger())
    assert await conn.health_check() is False
    try:
        await asyncio.wait_for(conn.run_until_eof(), timeout=5)
    except asyncio.TimeoutError:
        pytest.fail('_ConnectionHandler did not reach eof')
    assert await conn.health_check() is False
