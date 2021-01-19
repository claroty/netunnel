from netunnel.server.server import NETunnelServer

import asyncio


def main():
    # This will create a configuration file named 'netunnel.conf'
    server = NETunnelServer(config_path='netunnel.conf')
    loop = asyncio.get_event_loop()
    try:
        asyncio.ensure_future(server.start())
        loop.run_forever()
    finally:
        loop.close()


if __name__ == '__main__':
    main()
