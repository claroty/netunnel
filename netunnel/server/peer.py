from typing import Dict, List
from .static_tunnel import StaticTunnel
from .schemas import StaticTunnelSchema
from ..client import NETunnelClient
from ..common.utils import get_logger
from ..common.exceptions import NETunnelServerNotFound, NETunnelServerError, NETunnelResponseError, NETunnelAuthError
from ..common.auth import NETunnelClientAuth

import asyncio
import aiohttp


class Peer:
    def __init__(self, id, name, target_netunnel_url, auth, proxy_url=None, proxy_username=None, proxy_password=None, ssl=None, logger=None):
        """
        Peer is a remote NETunnelServer.
        :param id: unique id for this peer
        :param name: name of the peer
        :param target_netunnel_url: url to the remote netunnel server
        :param proxy_url: url to an http proxy to set when making http requests
        :param proxy_username: username for the proxy
        :param proxy_password: password for the proxy
        :param auth: Instance of subclass of netunnel.common.auth.NETunnelClientAuth that will be used to authenticate the peer
        :param ssl: SSLContext object. False to skip validation, None for default SSL check.
        :param logger: logging.Logger object for logging
        """
        self._id = id
        self.name = name
        self._target_netunnel_url = target_netunnel_url
        self._auth: NETunnelClientAuth = auth
        self._logger = logger or get_logger(f'Peer `{self.name}`')
        self._ssl = ssl
        self._proxy_url = proxy_url
        self._proxy_username = proxy_username
        self._proxy_password = proxy_password
        # mapping from static tunnel id to StaticTunnel object belong to this peer
        self._static_tunnels: Dict[int, StaticTunnel] = {}
        # Used to prevent id duplications when creating new static tunnels
        self._creating_static_tunnel_lock = asyncio.Lock()

    @property
    def id(self) -> int:
        return self._id

    @property
    def target_netunnel_url(self) -> str:
        return self._target_netunnel_url

    @property
    def auth(self):
        return self._auth

    @property
    def auth_data(self):
        return self._auth.dump_object()

    @property
    def static_tunnels(self) -> List[StaticTunnel]:
        """
        Return a list of the static tunnels to this peer. Used by the nested field of PeerSchema
        """
        return list(self._static_tunnels.values())

    async def update_settings(self, new_url, new_auth=None):
        """
        Set new settings for either target_netunnel_url, auth or both.
        Restart the static tunnels of this peer so they will use the new settings
        """
        if new_url:
            self._target_netunnel_url = new_url
        if new_auth:
            self._auth = new_auth
        for static_tunnel in self.static_tunnels:
            static_tunnel_settings = StaticTunnelSchema().dump3(static_tunnel)
            await self.delete_static_tunnel(static_tunnel.id)
            await self.add_static_tunnel(**static_tunnel_settings)

    def _generate_static_tunnel_id(self) -> int:
        """
        Generates an unused static tunnel id
        """
        if self._static_tunnels:
            return max(self._static_tunnels.keys()) + 1
        return 1

    def _new_client(self):
        """
        Return a NETunnelClient to the peer
        """
        return NETunnelClient(server_url=self._target_netunnel_url, proxy_url=self._proxy_url,
                               proxy_username=self._proxy_username, proxy_password=self._proxy_password,
                               auth_client=self._auth, ssl=self._ssl, logger=self._logger)

    async def verify_connectivity(self):
        """
        Make sure there is a connection to the peer by query it's version.
        Raises an exception if peer is not connected
        """
        try:
            async with self._new_client() as client:
                await client.get_remote_version()
        except NETunnelAuthError as err:
            self._logger.debug('The following exception raised when trying to connect to the peer:', exc_info=err)
            raise NETunnelAuthError(f'Failed to authenticate with peer `{self.name}`')
        except aiohttp.ClientError as err:
            self._logger.debug('The following exception raised when trying to connect to the peer:', exc_info=err)
            raise NETunnelServerError(f'Failed to connect with peer `{self.name}`')
        return True

    async def set_new_proxy(self, proxy_url, proxy_username, proxy_password):
        """
        Set a new http proxy to use when communicating with this peer
        """
        self._proxy_url = proxy_url
        self._proxy_username = proxy_username
        self._proxy_password = proxy_password
        for static_tunnel in self.static_tunnels:
            await static_tunnel.set_new_proxy(proxy_url, proxy_username, proxy_password)

    async def add_static_tunnel(self, tunnel_remote_address, tunnel_remote_port, tunnel_local_port, tunnel_local_address, id=None, verify_connectivity=True):
        """
        Creates a new static tunnel for this peer and start it.
        Return the generated static tunnel
        :param tunnel_remote_address: Remote address used as the exit address of the tunnel
        :param tunnel_remote_port: Remote port used as the exit port of the tunnel
        :param tunnel_local_address: Local address used as the entrance address of the tunnel
        :param tunnel_local_port: Local port used as the entrance port of the tunnel
        :param id: Optional id to set this tunnel. Used when tunnel is initialized from the config
        :param verify_connectivity: Whether to verify connectivity before adding the tunnel
        """
        if verify_connectivity:
            await self.verify_connectivity()
        async with self._creating_static_tunnel_lock:
            # Set static tunnel id
            static_tunnel_id = id or self._generate_static_tunnel_id()
            if id in self._static_tunnels:
                raise RuntimeError(f'ID `{id}` for static tunnel on peer `{self.name}` is already in use')

            # Create and start the new static tunnel
            static_tunnel = StaticTunnel(id=static_tunnel_id, tunnel_local_port=tunnel_local_port,
                                         tunnel_local_address=tunnel_local_address, tunnel_remote_port=tunnel_remote_port,
                                         tunnel_remote_address=tunnel_remote_address, target_netunnel_url=self._target_netunnel_url,
                                         auth_client=self._auth, proxy_url=self._proxy_url, proxy_username=self._proxy_username,
                                         proxy_password=self._proxy_password, ssl=self._ssl, logger=self._logger)
            self._logger.info('Creating static tunnel `%s` to peer `%s`', static_tunnel.get_tunnel_display_name(), self.name)
            await static_tunnel.start()
            await static_tunnel.wait_online()
            self._static_tunnels[static_tunnel_id] = static_tunnel
        return static_tunnel

    async def delete_static_tunnels(self):
        """
        Stop and remove all static tunnels
        """
        while self._static_tunnels:
            _, static_tunnel = self._static_tunnels.popitem()
            await static_tunnel.stop()

    async def delete_static_tunnel(self, id):
        """
        Remove static tunnel from this peer by id
        """
        if id not in self._static_tunnels:
            raise NETunnelServerNotFound(f'No static tunnel by id `{id}` on `{self.name}`')
        static_tunnel = self._static_tunnels.pop(id)
        await static_tunnel.stop()

    def get_static_tunnel(self, id):
        """
        Return a static tunnel by ID
        """
        if id not in self._static_tunnels:
            raise NETunnelServerNotFound(f'No static tunnel by id `{id}` on `{self.name}`')
        return self._static_tunnels[id]
