from netunnel.client import NETunnelClient

import asyncio


async def create_static_tunnel():
    async with NETunnelClient('http://localhost:4040') as client:
        peer = await client.register_peer('server2', target_netunnel_url='http://localhost:4041')
        static_tunnel = await client.create_peer_static_tunnel('server2', tunnel_remote_port=22)


if __name__ == '__main__':
    loop = asyncio.get_event_loop()
    loop.run_until_complete(create_static_tunnel())