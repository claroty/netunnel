from ..client import NETunnelClient
from ..common.tunnel import Tunnel
from ..common.utils import get_logger
from ..common.exceptions import NETunnelAuthError

import time
import aiohttp
import logging
import asyncio
import contextlib

CONNECTION_RETRY_INTERVAL = 10
HEALTH_CHECK_INTERVAL = 30


class StaticTunnel:
    def __init__(self, id, target_netunnel_url, tunnel_local_address, tunnel_local_port, tunnel_remote_address,
                 tunnel_remote_port, auth_client, proxy_url=None, proxy_username=None, proxy_password=None, ssl=None,
                 logger=None, connection_retry_interval=CONNECTION_RETRY_INTERVAL):
        """
        Interface for managing a static tunnel
        :param id: unique id for that static tunnel
        :param target_netunnel_url: url of the remote peer's netunnel server
        :param tunnel_local_address: local address to use for the static tunnel
        :param tunnel_local_port: local port to use for the static tunnel
        :param tunnel_remote_address: remote address to use for the static
        :param tunnel_remote_port: remote port to use for the static tunnel
        :param auth_client: Instance of subclass of netunnel.common.auth.NETunnelClientAuth that will be used to authenticate the remote
        :param proxy_url: url for a proxy to use when making requests to the remote netunnel server
        :param proxy_username: Optional username to use when authenticating with the proxy_url. `proxy_password` must be given as well
        :param proxy_password: Optional password to use when authenticating with the proxy_url. `proxy_username` must be given as well
        :param ssl: SSLContext object. False to skip validation, None for default SSL check.
        :param logger: logger to use
        :param connection_retry_interval: interval for retrying to connect the remote peer after failure
        """
        self._id = id
        self._client: NETunnelClient = NETunnelClient(server_url=target_netunnel_url, proxy_url=proxy_url,
                                                        proxy_username=proxy_username, proxy_password=proxy_password,
                                                        auth_client=auth_client, ssl=ssl, logger=logger)
        self._tunnel_remote_address = tunnel_remote_address
        self._tunnel_remote_port = tunnel_remote_port
        self._tunnel_local_address = tunnel_local_address
        self._tunnel_local_port = tunnel_local_port
        self._tunnel: Tunnel = None
        # _running is True when the static tunnel needs to be online
        self._running = False
        # This event is True when the static tunnel is actually online
        self._online_event = asyncio.Event()
        self._logger: logging.Logger = logger or get_logger(f'static_tunnel `{self._id}`')
        self._tunnel_running_task: asyncio.Future = None
        self._tunnel_watchdog_task: asyncio.Future = None
        self._connection_retry_interval = connection_retry_interval

    @property
    def id(self):
        return self._id

    @property
    def tunnel_local_address(self):
        return self._tunnel_local_address

    @property
    def tunnel_local_port(self):
        return self._tunnel_local_port

    @property
    def tunnel_remote_address(self):
        return self._tunnel_remote_address

    @property
    def tunnel_remote_port(self):
        return self._tunnel_remote_port

    @property
    def running(self):
        return self._running

    def get_tunnel_display_name(self):
        return f'local on {self._tunnel_local_address}:{self._tunnel_local_port} -> {self._client.server_url} on {self._tunnel_remote_address}:{self._tunnel_remote_port}'

    async def start(self):
        """
        Start connecting the tunnel to the peer. If peer is not available
        or the connection is lost, keep retrying
        """
        if self._running:
            raise RuntimeError('Static tunnel already started')
        self._running = True
        self._tunnel_running_task = asyncio.ensure_future(self._tunnel_keep_alive())
        self._tunnel_watchdog_task = asyncio.ensure_future(self._tunnel_watchdog())

    async def wait_online(self):
        """
        Blocks until the the static tunnel is up and running
        """
        await self._online_event.wait()

    async def set_new_proxy(self, proxy_url, proxy_username, proxy_password):
        """
        Set a new http proxy to the client and close the existing tunnel so it will recreate it with the new proxy
        """
        if not self._running:
            self._client.set_client_proxy(proxy_url, proxy_username, proxy_password)
            return
        try:
            await self.stop()
            self._client.set_client_proxy(proxy_url, proxy_username, proxy_password)
        finally:
            await self.start()

    async def _tunnel_watchdog(self):
        """
        Perform health checks to the tunnel every certain interval.
        if the tunnel marked as running but the health check fails, we stop it so that
        the _tunnel_keep_alive task can wake up and restart it
        """
        while self._running:
            if self._tunnel and self._tunnel.running and not await self._tunnel.health_check():
                # We use force because the tunnel is probably malfunctioning and might hang with graceful shutdown
                self._logger.info('Static Tunnel `%s` is not working. Restarting tunnel', self.get_tunnel_display_name())
                await self._tunnel.stop(force=True)
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)

    async def _tunnel_keep_alive(self):
        """
        try to connect to the peer and join the tunnel. If peer disconnected or unavailable,
        keep trying until closed.
        """
        while self._running:
            try:
                async with self._client:
                    start_time = time.time()
                    self._tunnel: Tunnel = await asyncio.wait_for(self._client.open_tunnel_to_server(remote_address=self._tunnel_remote_address,
                                                                                                     remote_port=self._tunnel_remote_port,
                                                                                                     local_address=self._tunnel_local_address,
                                                                                                     local_port=self._tunnel_local_port,
                                                                                                     wait_ready=True),
                                                                  self._connection_retry_interval)
                    self._logger.info('Static Tunnel `%s` is online', self.get_tunnel_display_name())
                    self._online_event.set()
                    await self._tunnel.join()
                self._online_event.clear()
                # At this point, the tunnel might have been closed unexpectedly so we break only if we're signaled to stop
                if self._running is False:
                    self._logger.info('Static Tunnel `%s` is closed', self.get_tunnel_display_name())
                    break
                self._logger.info('Static Tunnel `%s` is offline after %s seconds. Waiting %s seconds before reconnecting...',
                                  self.get_tunnel_display_name(), int(time.time() - start_time), self._connection_retry_interval)
                await asyncio.sleep(self._connection_retry_interval)
            except Exception as err:
                self._online_event.clear()
                if isinstance(err, aiohttp.ClientConnectionError):
                    self._logger.warning('Failed to connect to `%s`', self._client.server_url)
                elif isinstance(err, NETunnelAuthError):
                    self._logger.warning('Failed to authenticate with `%s`', self._client.server_url)
                else:
                    self._logger.exception('Failed to establish a tunnel with the peer `%s`:', self._client.server_url)
                self._logger.info('Waiting %s seconds before retrying...', self._connection_retry_interval)
                await asyncio.sleep(self._connection_retry_interval)


    async def stop(self, force=True):
        """
        Close the connection to the peer
        :param force: Whether to close the tunnel forcefully. Defaults to True
        """
        self._running = False
        if self._tunnel is not None and self._tunnel.running:
            await self._tunnel.stop(force=force)
            await self._tunnel_running_task
        if not self._tunnel_watchdog_task.done():
            # We cancel the watchdog task to avoid waiting for the sleep interval
            self._tunnel_watchdog_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tunnel_watchdog_task
        await self._client.close()