from typing import Tuple
from netunnel.client import NETunnelClient
from netunnel.server.server import NETunnelServer
from .utils import assert_tunnel_echo_server, assert_tunnel_not_listening

import pytest
import asyncio


class TestStaticTunnels:
    async def test_static_tunnel_startup(self, config_path, aiohttp_unused_port, bytes_data):
        """
        Test that static tunnels are working probably on startup
        """
        server_port = aiohttp_unused_port()
        port_to_tunnel = aiohttp_unused_port()
        server = NETunnelServer(config_path=config_path, port=server_port)
        await server.start()
        async with NETunnelClient(f'http://localhost:{server_port}') as client:
            peer = await client.register_peer('abc', target_netunnel_url=client.server_url)
            static_tunnel = await client.create_peer_static_tunnel(peer['name'], tunnel_remote_port=port_to_tunnel)
        # Assert static tunnel is working
        await assert_tunnel_echo_server(tunnel_entrance_address=static_tunnel['tunnel_local_address'],
                                        tunnel_entrance_port=static_tunnel['tunnel_local_port'],
                                        tunnel_exit_address=static_tunnel['tunnel_remote_address'],
                                        tunnel_exit_port=static_tunnel['tunnel_remote_port'],
                                        bytes_data=bytes_data)

        # Assert static tunnel stopped
        await server.stop()
        assert_tunnel_not_listening(static_tunnel['tunnel_local_address'], static_tunnel['tunnel_local_port'])
        # Assert static tunnel started working again
        await server.start()
        await server._peers[peer['id']]._static_tunnels[static_tunnel['id']].wait_online()
        await assert_tunnel_echo_server(tunnel_entrance_address=static_tunnel['tunnel_local_address'],
                                        tunnel_entrance_port=static_tunnel['tunnel_local_port'],
                                        tunnel_exit_address=static_tunnel['tunnel_remote_address'],
                                        tunnel_exit_port=static_tunnel['tunnel_remote_port'],
                                        bytes_data=bytes_data)

        # Cleanup
        await server.stop()

    async def test_static_tunnel_reconnect(self, netunnel_client_server: Tuple[NETunnelClient, NETunnelServer], config_path, aiohttp_unused_port, bytes_data):
        """
        Test that static tunnels successfully recreate themselves if there was a disconnection
        """
        client, server = netunnel_client_server

        port = aiohttp_unused_port()
        target_server = NETunnelServer(config_path=config_path, port=port)
        target_server_url = f'http://127.0.0.1:{port}'
        await target_server.start()

        tunnel_remote_port = aiohttp_unused_port()
        peer = await client.register_peer('abc', target_netunnel_url=target_server_url)
        tunnel_info = await client.create_peer_static_tunnel(peer['name'], tunnel_remote_port=tunnel_remote_port)

        static_tunnel_object = server._peers[peer['id']]._static_tunnels[tunnel_info['id']]
        # patch the connection retry interval so we won't have to wait for reconnection too long
        static_tunnel_object._connection_retry_interval = 1

        await assert_tunnel_echo_server(tunnel_entrance_address=tunnel_info['tunnel_local_address'],
                                        tunnel_entrance_port=tunnel_info['tunnel_local_port'],
                                        tunnel_exit_address=tunnel_info['tunnel_remote_address'],
                                        tunnel_exit_port=tunnel_info['tunnel_remote_port'],
                                        bytes_data=bytes_data)
        # Stop target server to cause disconnection and make sure it's disconnected
        await target_server.stop()
        assert_tunnel_not_listening(tunnel_info['tunnel_local_address'], tunnel_info['tunnel_local_port'])

        # Start target server to see if reconnection works
        await target_server.start()
        try:
            await asyncio.wait_for(static_tunnel_object.wait_online(), 2)
        except asyncio.TimeoutError:
            await static_tunnel_object.stop()
            pytest.fail('Static tunnel did not reconnect after 2 seconds')
        await assert_tunnel_echo_server(tunnel_entrance_address=tunnel_info['tunnel_local_address'],
                                        tunnel_entrance_port=tunnel_info['tunnel_local_port'],
                                        tunnel_exit_address=tunnel_info['tunnel_remote_address'],
                                        tunnel_exit_port=tunnel_info['tunnel_remote_port'],
                                        bytes_data=bytes_data)

        # cleanup
        await client.delete_peer_by_id(peer['id'])
        await target_server.stop()

