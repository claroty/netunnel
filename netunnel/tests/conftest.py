from typing import Tuple

from netunnel.server.server import NETunnelServer
from netunnel.client import NETunnelClient
from netunnel.common.utils import get_logger

import logging
import pytest
import tempfile
import time
import contextlib
import os


def pytest_configure(config):
    if config.pluginmanager.hasplugin('asyncio'):
        pytest.fail("Please uninstall pytest-asyncio first. We're using pytest-aiohttp and these packages conflicts with each other")


@contextlib.contextmanager
def config_path_context_manager():
    config_file_name = f'test_netunnel_config_{time.time()}.json'
    with tempfile.TemporaryDirectory() as config_dir_path:
        yield os.path.join(config_dir_path, config_file_name)


@pytest.fixture
def config_path():
    # This fixture is just a proxy to the context manager and not the implementation
    # because if it were, then test cases which needs both config_path and netunnel_client
    # fixtures together for example, will share the same config_path as the fixture netunnel_client
    with config_path_context_manager() as path:
        yield path


@pytest.fixture
async def netunnel_client(loop, aiohttp_unused_port):
    """
    Creates a client-server instance of netunnel and return the client.
    """
    with config_path_context_manager() as config_path:
        server_logger = get_logger('test_netunnel_server', logging.DEBUG)
        client_logger = get_logger('test_netunnel_client', logging.DEBUG)
        server = NETunnelServer(config_path=config_path, host='127.0.0.1', port=aiohttp_unused_port(), logger=server_logger)
        await server.start()
        server_url = f'http://localhost:{server._port}'
        async with NETunnelClient(server_url=server_url, logger=client_logger) as client:
            yield client
        await server.stop()


@pytest.fixture
async def netunnel_server(loop, aiohttp_unused_port):
    """
    Creates a NETunnelServer instance, start and return it.
    """
    with config_path_context_manager() as config_path:
        server_logger = get_logger('test_netunnel_server', logging.DEBUG)
        server = NETunnelServer(config_path=config_path, host='127.0.0.1', port=aiohttp_unused_port(), logger=server_logger)
        await server.start()
        yield server
        await server.stop()


@pytest.fixture
async def not_started_netunnel_client_server(loop, aiohttp_unused_port) -> Tuple[NETunnelClient, NETunnelServer]:
    """
    Creates a client-server instance of netunnel and return a tuple of (client, server)
    """
    with config_path_context_manager() as config_path:
        server_logger = get_logger('test_netunnel_server')
        client_logger = get_logger('test_netunnel_client')
        server = NETunnelServer(config_path=config_path, host='127.0.0.1', port=aiohttp_unused_port(), logger=server_logger)
        server_url = f'http://localhost:{server._port}'
        client = NETunnelClient(server_url=server_url, logger=client_logger)
        yield client, server


@pytest.fixture
async def netunnel_client_server(not_started_netunnel_client_server) -> Tuple[NETunnelClient, NETunnelServer]:
    """
    Creates a client-server instance of netunnel and return a tuple of (client, server)
    """
    client, server = not_started_netunnel_client_server
    await server.start()
    async with client as client:
        yield client, server
    await server.stop()


@pytest.fixture(scope="session")
def bytes_data():
    """
    Return a bytes object
    """
    return bytes(range(256))
