from netunnel.client import NETunnelClient
from netunnel.server.server import NETunnelServer
from netunnel import __version__
from .utils import assert_tunnel_echo_server

import socket
import asyncio


class TestNETunnelFlow:
    @staticmethod
    def _get_test_data(aiohttp_unused_port):
        return {
            'local_address': "127.0.0.1",
            'local_port': aiohttp_unused_port(),
            'remote_address': "127.0.0.1",
            'remote_port': aiohttp_unused_port()
        }

    @staticmethod
    async def assert_tunnel_no_remote_service(tunnel_entrance_host, tunnel_entrance_port, bytes_data):
        """
        Try connect to a tunnel without a remote service available
        """
        reader, writer = await asyncio.open_connection(host=tunnel_entrance_host, port=tunnel_entrance_port)
        writer.write(bytes_data)
        await writer.drain()
        assert await reader.read(1024) == b''  # Expected output when remote close the connection gracefully
        writer.close()

    async def assert_tunnel_working(self, tunnel, bytes_data):
        # Check if tunnel is listens
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            assert sock.connect_ex(tunnel.get_entrance_socket()) == 0, f"Tunnel not listening on {tunnel.get_entrance_socket()}"

        # Try to connect to the tunnel without a "remote" service to accept the connection
        entrance_host, entrance_port = tunnel.get_entrance_socket()
        await asyncio.wait_for(self.assert_tunnel_no_remote_service(entrance_host, entrance_port, bytes_data), 2)

        # # Create a "remote" service to accept the connection and try again
        exit_host, exit_port = tunnel.get_exit_socket()
        await asyncio.wait_for(assert_tunnel_echo_server(entrance_host, entrance_port, exit_host, exit_port, bytes_data), 2)

    async def test_tunnel_client_to_server(self, netunnel_client: NETunnelClient, aiohttp_unused_port, bytes_data):
        tunnel = await netunnel_client.open_tunnel_to_server(**self._get_test_data(aiohttp_unused_port))
        await self.assert_tunnel_working(tunnel, bytes_data)
        await tunnel.stop()

    async def test_tunnel_server_to_client(self, netunnel_client: NETunnelClient, aiohttp_unused_port, bytes_data):
        tunnel = await netunnel_client.open_tunnel_to_client(**self._get_test_data(aiohttp_unused_port))
        await self.assert_tunnel_working(tunnel, bytes_data)
        await tunnel.stop()

    async def test_netunnel_client_reuse(self, netunnel_server: NETunnelServer, aiohttp_unused_port):
        """Check that netunnel client release whatever it needs to be used again"""
        netunnel_url = f'http://127.0.0.1:{netunnel_server._port}'
        client = NETunnelClient(server_url=netunnel_url)
        async with client:
            await client.open_tunnel_to_client(**self._get_test_data(aiohttp_unused_port))
        async with client:
            await client.open_tunnel_to_client(**self._get_test_data(aiohttp_unused_port))

    async def test_get_remote_version(self, netunnel_client: NETunnelClient):
        assert __version__ == await netunnel_client.get_remote_version()

    async def test_static_tunnels(self, netunnel_client: NETunnelClient, bytes_data, aiohttp_unused_port):
        tunnel_remote_port = aiohttp_unused_port()
        peer = await netunnel_client.register_peer('abc', target_netunnel_url=netunnel_client.server_url)
        static_tunnel = await netunnel_client.create_peer_static_tunnel(peer['name'], tunnel_remote_port=tunnel_remote_port)
        try:
            await assert_tunnel_echo_server(tunnel_entrance_address=static_tunnel['tunnel_local_address'], tunnel_entrance_port=static_tunnel['tunnel_local_port'],
                                            tunnel_exit_address='127.0.0.1', tunnel_exit_port=tunnel_remote_port, bytes_data=bytes_data)
        finally:
            await netunnel_client.delete_peer_by_id(peer['id'])
