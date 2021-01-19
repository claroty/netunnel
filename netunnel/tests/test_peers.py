from typing import Tuple
from netunnel.client import NETunnelClient
from netunnel.server.server import NETunnelServer
from netunnel.common import exceptions
from netunnel.common.utils import get_logger
from .auth_utils import MockServerAuth, MockClientAuth

import pytest


class TestPeers:
    async def test_peers_rest_api(self, netunnel_client_server: Tuple[NETunnelClient, NETunnelServer]):
        client, server = netunnel_client_server
        assert len(await client.list_peers()) == 0
        peer1 = await client.register_peer(name='peer1', target_netunnel_url=client.server_url)
        # Test unique name
        with pytest.raises(exceptions.NETunnelError):
            await client.register_peer(name='peer1', target_netunnel_url=client.server_url)
        assert len(await client.list_peers()) == 1
        # Test creation of another peer and delete it
        peer2 = await client.register_peer(name='peer2', target_netunnel_url=client.server_url)
        assert len(await client.list_peers()) == 2
        await client.delete_peer_by_id(peer2['id'])
        # Test the remaining peer is really the first one
        peers = await client.list_peers()
        assert len(peers) == 1
        assert peers[0] == peer1
        # Test GET requests for exists and non-exists peers
        assert await client.get_peer_by_id(peer1['id']) == peer1
        assert await client.get_peer_by_name(peer1['name']) == peer1
        with pytest.raises(exceptions.NETunnelError):
            await client.get_peer_by_id(999)
        with pytest.raises(exceptions.NETunnelError):
            await client.get_peer_by_name('non-existing-peer')
        # Test update peer name
        new_peer = await client.update_peer(peer1['id'], new_name='new_peer1')
        assert peer1['id'] == new_peer['id'] and new_peer['name'] == 'new_peer1'
        peer1 = new_peer
        # Test exception in update peer doesn't partially change it
        with pytest.raises(exceptions.NETunnelError):
            await client.update_peer(peer1['id'], new_name='unused_name', new_target_netunnel_url='http://non.existings.url.com/')
        assert await client.get_peer_by_id(peer1['id']) == peer1
        # Test static tunnels API
        assert len(await client.list_peer_static_tunnels(peer1['name'])) == 0
        static_tunnel1 = await client.create_peer_static_tunnel(peer_name=peer1['name'], tunnel_remote_port=22)
        assert len(await client.list_peer_static_tunnels(peer1['name'])) == 1
        static_tunnel2 = await client.create_peer_static_tunnel(peer_name=peer1['name'], tunnel_remote_port=22)
        assert len(await client.list_peer_static_tunnels(peer1['name'])) == 2
        await client.delete_peer_static_tunnel(peer_name=peer1['name'], static_tunnel_id=static_tunnel2['id'])
        static_tunnels = await client.list_peer_static_tunnels(peer1['name'])
        assert len(static_tunnels) == 1
        assert static_tunnels[0] == static_tunnel1
        assert await client.get_peer_static_tunnel(peer_name=peer1['name'], static_tunnel_id=static_tunnel1['id']) == static_tunnel1
        await client.delete_peer_by_id(peer1['id'])

    async def test_update_target_url(self, netunnel_client_server: Tuple[NETunnelClient, NETunnelServer]):
        client, server = netunnel_client_server
        peer = await client.register_peer(name='peer1', target_netunnel_url=client.server_url)
        static_tunnel = await client.create_peer_static_tunnel(peer_name=peer['name'], tunnel_remote_port=22)
        assert server._peers[peer['id']]._static_tunnels[static_tunnel['id']]._client.server_url == client.server_url
        with pytest.raises(exceptions.NETunnelError):
            await client.update_peer(peer['id'], new_target_netunnel_url='http://non.existings.url.com/')
        # We manually change the url to skip the validation, and then we test setting it to the valid one
        server._peers[peer['id']]._static_tunnels[static_tunnel['id']]._client.server_url = 'http://non.existings.url.com/'
        await client.update_peer(peer['id'], new_target_netunnel_url=client.server_url)
        assert server._peers[peer['id']]._static_tunnels[static_tunnel['id']]._client.server_url == client.server_url

    async def test_authenticated_peer(self, config_path, aiohttp_unused_port):
        logger = get_logger('test_authenticated_peer')
        auth_client = MockClientAuth(secret='hlu')
        auth_server = MockServerAuth(secret='hlu')
        server = NETunnelServer(config_path, port=aiohttp_unused_port(), auth_server=auth_server, logger=logger)
        await server.start()
        async with NETunnelClient(f'http://localhost:{server._port}', auth_client=auth_client, logger=logger) as client:
            # Make sure the authentication work with the server
            await client.get_remote_version()
            # Make sure the authentication does not work when registering peer (because we didn't provide data)
            with pytest.raises(exceptions.NETunnelResponseError):
                await client.register_peer(name='peer1', target_netunnel_url=client.server_url)
            # Make sure it does not work when we provide invalid data
            auth_data = {'secret': 'invalid'}
            with pytest.raises(exceptions.NETunnelResponseError):
                await client.register_peer(name='peer1', target_netunnel_url=client.server_url, auth_data=auth_data)
            # Make sure it works now that provide valid data
            auth_data = {'secret': 'hlu'}
            await client.register_peer(name='peer1', target_netunnel_url=client.server_url, auth_data=auth_data)
        await server.stop()
