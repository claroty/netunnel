from typing import Tuple
from urllib.parse import urlparse
from netunnel.client import NETunnelClient
from netunnel.server.config import get_default_config
from netunnel.server.server import NETunnelServer, SECRET_STRING
from netunnel.common.exceptions import NETunnelResponseError
from netunnel.common.utils import get_logger
from .utils import ProxyForTests
from .auth_utils import MockClientAuth, MockServerAuth

import copy
import json
import pytest


class TestNETunnelServer:
    async def test_server_can_close_tunnels_from_client(self, netunnel_client_server: Tuple[NETunnelClient, NETunnelServer], aiohttp_unused_port):
        client, server = netunnel_client_server
        tunnel = await client.open_tunnel_to_server(remote_address='127.0.0.1', remote_port=aiohttp_unused_port())
        assert tunnel.running
        tunnel_id = list(client._tunnels.keys())[0]
        await server.channel_handlers[client._control_channel.id]._tunnels[tunnel_id].stop()
        assert tunnel.running is False

    @pytest.mark.parametrize('allowed_dests_config, dest_address, legit_ports, not_legit_ports',
                             [({"127.0.0.1": "22"}, "127.0.0.1", [22], [23, 24, 21]),
                                ({"127.0.0.1": ""}, "127.0.0.1", [], [22, 23, 24, 21]),
                                ({"127.0.0.1": "22, 23"}, "127.0.0.1", [22, 23], [24, 21]),
                                ({"127.0.0.1": "*"}, "127.0.0.1", [1, 2, 22, 443], []),
                                (None, "127.0.0.1", [1, 2, 22, 443], []),
                                ({"8.8.8.8": "*"}, "127.0.0.1", [], [22, 443, 5000]),
                                ({"8.8.8.8": "22,443"}, "8.8.8.8", [22, 443], [5000]),
                                ],)
    async def test_server_allowed_tunnel_destinations(self, not_started_netunnel_client_server: Tuple[NETunnelClient, NETunnelServer],
                                                      allowed_dests_config, dest_address, legit_ports, not_legit_ports):
        client, server = not_started_netunnel_client_server
        if allowed_dests_config:
            with open(server._config_path, 'w') as f:
                test_config = get_default_config()
                test_config.update({'allowed_tunnel_destinations': allowed_dests_config})
                json.dump(test_config, f)
        await server.start()
        async with client as client:
            # verify allowed ports are working
            for port in legit_ports:
                tunnel = await client.open_tunnel_to_server(remote_address=dest_address, remote_port=port)
                assert tunnel.running
            # verify not allowed ports are not working
            for port in not_legit_ports:
                with pytest.raises(NETunnelResponseError):
                    await client.open_tunnel_to_server(remote_address=dest_address, remote_port=port)
        await server.stop()

    @pytest.mark.parametrize("username,password", [(None, None), ('abc', 'abc')])
    async def test_default_http_proxy(self, username, password, netunnel_client_server: Tuple[NETunnelClient, NETunnelServer], aiohttp_unused_port):
        test_url = 'http://www.google.com/'
        test_url_hostname = urlparse(test_url).hostname
        client, server = netunnel_client_server
        current_http_proxy_settings = await client.get_server_default_http_proxy()
        assert current_http_proxy_settings == {}
        proxy_port = aiohttp_unused_port()
        proxy_url = f'http://localhost:{proxy_port}'
        with ProxyForTests(port=proxy_port, username=username, password=password) as proxy:
            new_http_proxy_settings = await client.set_server_default_http_proxy(proxy_url=proxy_url, username=username, password=password, test_url=test_url)
            proxy.assert_host_forwarded(test_url_hostname)
        assert new_http_proxy_settings['proxy_url'] == proxy_url
        # If username and password has value, we expect the response to be censored
        assert new_http_proxy_settings['username'] == (username and SECRET_STRING)
        assert new_http_proxy_settings['password'] == (password and SECRET_STRING)
        unverified_http_proxy_settings = await client.set_server_default_http_proxy(proxy_url='', username=username, password=password, check_proxy=False)
        assert unverified_http_proxy_settings['proxy_url'] == ''
        assert unverified_http_proxy_settings['username'] == (username and SECRET_STRING)
        assert unverified_http_proxy_settings['password'] == (password and SECRET_STRING)
        empty_http_proxy_settings = await client.set_server_default_http_proxy(proxy_url=None)
        assert empty_http_proxy_settings == {}
        tunnel_remote_port = aiohttp_unused_port()
        peer_settings = await client.register_peer(name='abc', target_netunnel_url=client.server_url)
        static_tunnel_settings = await client.create_peer_static_tunnel(peer_name='abc', tunnel_remote_port=tunnel_remote_port)
        proxy_port = aiohttp_unused_port()
        proxy_url = f'http://localhost:{proxy_port}'
        try:
            with ProxyForTests(port=proxy_port, username=username, password=password) as proxy:
                await client.set_server_default_http_proxy(proxy_url=proxy_url, username=username, password=password, test_url=test_url)
                static_tunnel = server._peers[peer_settings['id']]._static_tunnels[static_tunnel_settings['id']]
                assert static_tunnel._client._proxy_url == proxy_url
                proxy.assert_host_forwarded(test_url_hostname)
        finally:
            await client.delete_peer_by_id(peer_settings['id'])

    async def test_setup_encryptor(self, netunnel_server: NETunnelServer):
        """
        Test that the encryptor is setup correctly before and after it was set again.
        """
        assert 'secret_key' in netunnel_server._config
        original_auto_generated_secret_key = netunnel_server._config['secret_key']
        await netunnel_server._setup_encryptor()
        assert original_auto_generated_secret_key == netunnel_server._config['secret_key']

    async def test_authentication(self, config_path, aiohttp_unused_port):
        logger = get_logger('test_authentication')
        auth_client = MockClientAuth(secret='hlu')
        auth_server = MockServerAuth(secret='hlu')
        server = NETunnelServer(config_path, port=aiohttp_unused_port(), auth_server=auth_server, logger=logger)
        await server.start()
        with pytest.raises(NETunnelResponseError):
            async with NETunnelClient(f'http://localhost:{server._port}', logger=logger) as client:
                await client.get_remote_version()
        async with NETunnelClient(f'http://localhost:{server._port}', auth_client=auth_client, logger=logger) as client:
            await client.get_remote_version()
        await server.stop()

    async def test_factory_reset(self, netunnel_client_server: Tuple[NETunnelClient, NETunnelServer]):
        client, server = netunnel_client_server
        changable_config_keys = ['secret_key']
        original_config = copy.deepcopy(server._config._config)

        # Fill up some data and make sure the configuration got updated
        await client.register_peer('peer', target_netunnel_url=client.server_url)
        await client.create_peer_static_tunnel(peer_name='peer', tunnel_remote_port=22)
        await client.set_server_default_http_proxy(proxy_url='http://127.0.0.1:8899', username='abc', password='abc',
                                                   check_proxy=False)
        assert server._config._config != original_config

        # Perform factory reset and make sure it worked as expected
        await client.factory_reset()
        new_config = copy.deepcopy(server._config._config)
        for key in changable_config_keys:
            assert original_config.pop(key) != new_config.pop(key)
        assert original_config == new_config

        # Perform factory reset which disconnect all clients
        assert client.connected
        await client.factory_reset(disconnect_clients=True)
        assert not client.connected
