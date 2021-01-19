from netunnel.client import NETunnelClient

import asyncio


async def main():
    async with NETunnelClient('http://localhost:4040') as client:
        # Open a client-to-server tunnel from localhost:12345 to localhost:22
        tunnel = await client.open_tunnel_to_server(local_address='127.0.0.1',
                                                    local_port=12345,
                                                    remote_address='127.0.0.1',
                                                    remote_port=22)
        await tunnel.join()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.run_until_complete(main())
