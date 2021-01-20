import json
import aiohttp
import asyncio
import logging
import argparse
import importlib
import functools
import contextlib

from typing import Dict, List, Any
from . import __version__
from .common import auth
from .common.const import TunnelId, CLIENT_CHANNEL_HEARTBEAT
from .common.exceptions import NETunnelResponseError, NETunnelNotConnectedError, NETunnelError
from .common.utils import get_logger, task_in_list_until_done, EventClientWebSocketResponse, get_session, asyncio_all_tasks
from .common.tunnel import InputTunnel, OutputTunnel, Tunnel
from .common.channel import Channel, ChannelMessage, ChannelResponse, Messages


def connection_established(async_method):
    @functools.wraps(async_method)
    async def wrap(self: 'NETunnelClient', *args, **kwargs):
        if self.connected is False:
            raise NETunnelNotConnectedError('Client is not connected. Please use `async with NETunnelClient(...):` to avoid this exception')
        return await async_method(self, *args, **kwargs)
    return wrap


class NETunnelClient:
    def __init__(self, server_url, logger=None, proxy_url=None, proxy_username=None, proxy_password=None, auth_client: auth.NETunnelClientAuth = None,
                 ssl=None):
        """
        Client-side object to communicate with a netunnel server.
        A constant websocket is used to allow the server to perform requests to the client
        :param server_url: URL to the remote netunnel server
        :param logger: optional logger
        :param proxy_url: url for a proxy to use when making requests to the remote netunnel server
        :param proxy_username: Optional username to use when authenticating with the proxy_url. `proxy_password` must be given as well
        :param proxy_password: Optional password to use when authenticating with the proxy_url. `proxy_username` must be given as well
        :param auth_client: Instance of subclass of netunnel.common.auth.NETunnelClientAuth that will be used for authentication
        :param ssl: SSLContext object. False to skip validation, None for default SSL check.
        """
        self.server_url = server_url
        self._proxy_url = None
        self._proxy_auth = None
        self.set_client_proxy(proxy_url, proxy_username, proxy_password)
        # aiohttp.ClientSession must be initialized from inside coroutine.
        # DO NOT USE __client_session.get/post/etc.. use await self._get / await self._post instead.
        self.__client_session: aiohttp.ClientSession = None
        self._control_channel: Channel = None
        self._control_channel_task: asyncio.Task = None
        self._tunnels: Dict[TunnelId, Tunnel] = {}
        # As its name implies, this lock is used when creating server-to-client tunnels. This resolves a
        # race condition when the server request a websocket for a newly created tunnel_id, but the http post
        # request for creating this tunnel_id is still processing, so the client did not register the new tunnel_id yet.
        self._tunnel_to_client_is_under_construction_lock = asyncio.Lock()
        # list of tasks to handle new websockets for existing tunnels. Used for cleanups
        self._tunnels_connections_tasks: List[asyncio.Task] = []
        self._logger = logger or get_logger(__name__)
        self._auth_client: auth.NETunnelClientAuth = auth_client or auth.ClientNoAuth()
        self._ssl = ssl

    def _get_url(self, uri: str) -> str:
        return f'{self.server_url}{uri}'

    async def _get(self, uri: str, parse_as_text=False, *args, **kwargs):
        """
        Perform GET request to server_url with NETunnelClient settings
        :param uri: The uri to append server_url
        :param parse_as_text: parse the response as text instead of json
        """
        url = self._get_url(uri)
        auth_headers = await self._request_auth_headers()
        async with self.__client_session.get(url, *args, headers=auth_headers, proxy=self._proxy_url, proxy_auth=self._proxy_auth, **kwargs) as response:
            if parse_as_text:
                return await response.text()
            return await self._parse_response(response)

    async def _post(self, uri: str, *args, **kwargs):
        """
        Perform POST request to server_url with NETunnelClient settings
        """
        url = self._get_url(uri)
        auth_headers = await self._request_auth_headers()
        async with self.__client_session.post(url, *args, headers=auth_headers, proxy=self._proxy_url, proxy_auth=self._proxy_auth, **kwargs) as response:
            return await self._parse_response(response)

    async def _delete(self, uri: str, **kwargs):
        """
        Perform DELETE request to server_url with NETunnelClient settings
        """
        url = self._get_url(uri)
        auth_headers = await self._request_auth_headers()
        async with self.__client_session.delete(url, headers=auth_headers, proxy=self._proxy_url, proxy_auth=self._proxy_auth, **kwargs) as response:
            return await self._parse_response(response)

    async def _ws_connect(self, uri: str, **kwargs):
        """
        Perform websocket connection to server_url with NETunnelClient settings.
        Usage:
        async with (await self._ws_connect(<uri>)) as websocket:
            websocket.send_bytes(...)
        """
        url = self._get_url(uri)
        auth_headers = await self._request_auth_headers()
        return self.__client_session.ws_connect(url, headers=auth_headers, heartbeat=CLIENT_CHANNEL_HEARTBEAT,
                                                proxy=self._proxy_url, proxy_auth=self._proxy_auth, **kwargs)

    async def _request_auth_headers(self) -> dict:
        """
        Return headers authorized by the remote server.
        We first authenticate if we're not already.
        """
        if not await self._auth_client.is_authenticated():
            await self._auth_client.authenticate(client=self)
        return await self._auth_client.get_authorized_headers()

    def set_client_proxy(self, proxy_url, proxy_username=None, proxy_password=None):
        """
        Set the client's proxy to use when making requests to the netunnel server
        :param proxy_url: new url for a proxy to use when making requests to the remote netunnel server
        :param proxy_username: Optional username to use when authenticating with the proxy_url. `proxy_password` must be given as well
        :param proxy_password: Optional password to use when authenticating with the proxy_url. `proxy_username` must be given as well
        """
        if proxy_url is None:
            self._proxy_url = None
            self._proxy_auth = None
            return
        self._proxy_url = proxy_url
        if proxy_username and proxy_password:
            self._proxy_auth = aiohttp.BasicAuth(proxy_username, proxy_password)

    @staticmethod
    async def _parse_response(response: aiohttp.ClientResponse) -> Any:
        """
        Try await for json response. If there is an error message, raise ClientResponseError with
        the error message instead of the default status code error
        """
        try:
            data = await response.json()
            if data is not None and 'error' in data:
                raise NETunnelResponseError(data['error'])
            return data
        except aiohttp.ContentTypeError:
            raise NETunnelResponseError(await response.text())

    @connection_established
    async def get_remote_version(self):
        return await self._get('/version', parse_as_text=True, raise_for_status=True)

    @property
    def connected(self):
        return self._control_channel is not None and self._control_channel.running

    @property
    def ssl(self):
        return self._ssl

    async def connect(self):
        """
        Establish a channel with the server. This channel is mandatory for some of the
        functionality of NETunnelClient, like opening tunnels.
        """
        if self.connected:
            raise RuntimeError(f'Client already connected to {self.server_url}')
        headers = await self._request_auth_headers()
        self.__client_session = await get_session(headers=headers, ssl=self._ssl)
        channel_payload = {'version': __version__}
        self._logger.debug('Connecting to `%s`', self.server_url)
        channel_data = await self._post('/channels', json=channel_payload)
        channel_id = channel_data['channel_id']
        remote_version = channel_data['version']
        # aiohttp websockets supposed to be called only with `async with`, but
        # we need the websocket to be constantly open and we want to avoid
        # breaking compatibility in the future therefore we call `.__aenter__()` directly.
        # Channel class will close the websocket when it finishes
        websocket_async_context_manager = await self._ws_connect(f'/channels/{channel_id}/connect')
        self._logger.debug('Starting channel `%s`. Server version: `%s`', channel_id, remote_version)
        websocket: EventClientWebSocketResponse = await websocket_async_context_manager.__aenter__()
        self._control_channel = Channel(websocket=websocket, channel_id=channel_id, handler=self._handle_server_messages, logger=self._logger)

        # Start serving on an external task so we can continue using the client
        self._control_channel_task = asyncio.ensure_future(self._serve_channel())

    async def _serve_channel(self):
        """
        Start serving the channel and cleanup after it closes
        """
        try:
            await self._control_channel.serve()
        finally:
            self._logger.debug('Finished serving channel `%s` on `%s`. Cleaning any remaining tunnels', self._control_channel.id, self.server_url)
            # The tunnel's close_callback will delete from self._tunnels, so to avoid RuntimeError for changing dictionary while iteration, we cast to list first
            for tunnel_id, tunnel in list(self._tunnels.items()):
                self._logger.debug('Stopping tunnel `%s`', tunnel_id)
                # The channel is closed so the server is responsible for closing its own tunnels
                await tunnel.stop(stop_remote_tunnel=False, force=True)
            while self._tunnels_connections_tasks:
                task = self._tunnels_connections_tasks.pop()
                with contextlib.suppress(asyncio.CancelledError):
                    task.cancel()
                    await task

    async def _handle_server_messages(self, message: ChannelMessage) -> ChannelResponse:
        """
        Handle messages that comes from the control channel
        """
        self._logger.debug('New message from `%s` on channel `%s`: %s', self.server_url, self._control_channel.id, message.message_type.name)
        if message.message_type == Messages.TUNNEL_SERVER_TO_CLIENT_NEW_CONNECTION:
            tunnel_id = message.data['tunnel_id']
            self._logger.debug('Creating new connection for server-to-client tunnel `%s`', tunnel_id)
            # This lock ensure that all server-to-client tunnels are ready and registered
            async with self._tunnel_to_client_is_under_construction_lock:
                if tunnel_id not in self._tunnels:
                    return message.get_error_response(f'tunnel_id `{tunnel_id}` is not registered on this client')
                new_connection_task = asyncio.ensure_future(self._serve_tunnel_new_websocket_connection(tunnel_id))
                task_in_list_until_done(new_connection_task, self._tunnels_connections_tasks)
                return message.get_valid_response()
        elif message.message_type == Messages.DELETE_TUNNEL:
            tunnel_id = message.data['tunnel_id']
            force = message.data['force']
            self._logger.debug('Closing tunnel `%s` %s',
                               tunnel_id, 'forcefully' if force else 'gracefully')
            # Request to stop the tunnel came from remote, so obviously we stop it only on our side
            await self._tunnels[tunnel_id].stop(stop_remote_tunnel=False, force=force)
            return message.get_valid_response()
        return message.get_error_response(f'Unknown message_type {message.message_type}')

    async def close(self):
        """
        Close the client sessions and control channel if its connected and await for the cleanup afterwards.
        """
        self._logger.debug('Closing NETunnel client to `%s`', self.server_url)
        try:
            if self.connected:
                self._logger.debug('Closing channel to `%s`', self.server_url)
                await self._control_channel.close()
                await self._control_channel_task
        finally:
            await self._close_session()

    async def _close_session(self):
        if self.__client_session and not self.__client_session.closed:
            await self.__client_session.close()

    @connection_established
    async def open_tunnel_to_server(self, remote_address, remote_port, local_address='127.0.0.1', local_port=None, wait_ready=True) -> Tunnel:
        """
        Creates a tunnel from here to the server (client-to-server tunnel).
        :param remote_address: address on the remote to communicate with
        :param remote_port: port on the remote to communicate with
        :param local_address: address to listen on locally. Defaults to localhost
        :param local_port: port to listen on locally. Defaults to random
        :param wait_ready: return tunnel only after it's ready to receive websockets
        :return: Tunnel object
        """
        payload = {'exit_address': remote_address, 'exit_port': remote_port, 'reverse': False}
        data = await self._post(f'/channels/{self._control_channel.id}/tunnels', json=payload)
        tunnel_id = data['tunnel_id']
        self._tunnels[tunnel_id] = InputTunnel(entrance_address=local_address, entrance_port=local_port,
                                               websocket_feeder_coro=self._input_tunnel_websocket_feeder(tunnel_id),
                                               exit_address=remote_address, exit_port=remote_port,
                                               logger=self._logger, stop_tunnel_on_remote_callback=lambda force: self._delete_tunnel_on_server(tunnel_id, force),
                                               stopped_callback=lambda: self._delete_tunnel_from_tunnels(tunnel_id))
        await self._tunnels[tunnel_id].start()
        if wait_ready:
            await self._tunnels[tunnel_id].wait_new_websocket()
        return self._tunnels[tunnel_id]

    @connection_established
    async def open_tunnel_to_client(self, local_address, local_port, remote_address='127.0.0.1', remote_port=None) -> Tunnel:
        """
        Creates a tunnel from the server to here. (server-to-client tunnel)
        :param local_address: local connection address. This is the exit address of the tunnel
        :param local_port: local connection port. This is the exit port of the tunnel
        :param remote_address: address on the server to listen. Defaults to localhost
        :param remote_port: port on the server to listen. Defaults to random.
        """
        if not self.connected:
            raise RuntimeError('Cannot create tunnel to client without connecting first. Use `async with NETunnelClient` to avoid this exception')
        payload = {'entrance_address': remote_address, 'entrance_port': remote_port, 'reverse': True}
        async with self._tunnel_to_client_is_under_construction_lock:
            data = await self._post(f'/channels/{self._control_channel.id}/tunnels', json=payload)
            tunnel_id = data['tunnel_id']
            self._tunnels[tunnel_id] = OutputTunnel(entrance_address=remote_address, entrance_port=remote_port,
                                                    exit_address=local_address, exit_port=local_port,
                                                    logger=self._logger, stop_tunnel_on_remote_callback=lambda force: self._delete_tunnel_on_server(tunnel_id, force),
                                                    stopped_callback=lambda: self._delete_tunnel_from_tunnels(tunnel_id))
            await self._tunnels[tunnel_id].start()
        return self._tunnels[tunnel_id]

    async def _delete_tunnel_on_server(self, tunnel_id, force):
        """
        Used internally to close tunnels. This is used as a callback for Tunnel.close() so
        that we can request the server to close the tunnel on its side, and also
        as a cleanup for self._tunnels
        :param tunnel_id: Tunnel ID on the server to delete
        :param force: Whether to stop the tunnel forcefully or to wait for connections to finish
        """
        self._logger.debug('Deleting tunnel `%s` of channel `%s` on `%s`', tunnel_id, self._control_channel.id, self.server_url)
        try:
            payload = {'force': force}
            await self._delete(f'/channels/{self._control_channel.id}/tunnels/{tunnel_id}', json=payload)
        except aiohttp.ClientError as err:
            self._logger.warning('Failed to delete remote tunnel `%s`: %s', tunnel_id, err)

    async def _delete_tunnel_from_tunnels(self, tunnel_id):
        del self._tunnels[tunnel_id]

    async def _input_tunnel_websocket_feeder(self, tunnel_id):
        """
        As long as the tunnel is alive, feed it with websockets.
        We wait until the pending websocket is consumed and create another one to reduce latency
        on new connections.
        :param tunnel_id: ID of the tunnel to feed
        """
        tunnel = self._tunnels[tunnel_id]
        while tunnel.running:
            await tunnel.wait_no_websocket()
            if not tunnel.running:
                break
            self._logger.debug('Generating new websocket for tunnel `%s` of channel `%s` on `%s`', tunnel_id, self._control_channel.id, self.server_url)
            new_connection_task = asyncio.ensure_future(self._serve_tunnel_new_websocket_connection(tunnel_id))
            task_in_list_until_done(new_connection_task, self._tunnels_connections_tasks)
            try:
                await asyncio.wait_for(tunnel.wait_new_websocket(), timeout=10)
            except asyncio.TimeoutError:
                self._logger.warning('Failed to retrieve websocket on Tunnel `%s` after 10 seconds. Retrying..', tunnel_id)

    async def _serve_tunnel_new_websocket_connection(self, tunnel_id):
        """
        Used internally. Creates a new websocket and feed it to the tunnel.
        Awaits until the websocket is used up before closing it.
        :param tunnel_id: ID of the tunnel to serve
        """
        async with (await self._ws_connect(f'/channels/{self._control_channel.id}/tunnels/{tunnel_id}/connect')) as websocket:
            websocket: EventClientWebSocketResponse
            await self._tunnels[tunnel_id].feed_websocket(websocket)
            await websocket.wait_closed()

    @connection_established
    async def list_peers(self) -> List[Any]:
        """
        Return a list of registered peers on the remote netunnel server.
        """
        return await self._get('/peers')

    @connection_established
    async def register_peer(self, name, target_netunnel_url, auth_data=None):
        """
        Register a new peer on the remote netunnel server.
        The peer is stored on the server and can be used to set static tunnels
        :param name: name of the remote peer. This will be used as an identifier of the peer
        :param target_netunnel_url: url to the remote netunnel server on the peer (e.g. https://my.netunnel.server/netunnel)
        :param auth_data: data used to authenticate with this peer
        """
        peer_payload = {
            'name': name,
            'target_netunnel_url': target_netunnel_url,
            'auth_data': auth_data or {}
        }
        return await self._post('/peers', json=peer_payload)

    @connection_established
    async def update_peer(self, peer_id, new_name=None, new_target_netunnel_url=None, auth_data=None):
        """
        Update peer fields
        Only updating the target_netunnel_url will trigger restart to the static tunnels of this peer
        :param peer_id: The id of the peer to update
        :param new_name: new name for the peer
        :param new_target_netunnel_url: new url to use when making requests to this peer. (Will trigger all static tunnels to restart)
        :param auth_data: new data required for authentication
        """
        update_peer_payload = {}
        if new_name:
            update_peer_payload['name'] = new_name
        if new_target_netunnel_url:
            update_peer_payload['target_netunnel_url'] = new_target_netunnel_url
        if auth_data:
            update_peer_payload['auth_data'] = auth_data
        return await self._post(f'/peers/{peer_id}', json=update_peer_payload)

    @connection_established
    async def delete_peer_by_id(self, peer_id):
        """
        Delete a peer by an id
        :param peer_id: The id of the peer to delete
        """
        return await self._delete(f'/peers/{peer_id}')

    @connection_established
    async def delete_peer_by_name(self, name):
        """
        Delete a peer by it's name identifier
        :param name: name of the peer to delete
        """
        peers = await self.list_peers()
        for peer in peers:
            if peer['name'] == name:
                return await self.delete_peer_by_id(peer['id'])
        raise NETunnelError(f'Peer `{name}` was not found')

    @connection_established
    async def get_peer_by_id(self, peer_id) -> Dict[str, Any]:
        """
        Return peer settings by an id
        :param peer_id: The id of the peer to return
        """
        return await self._get(f'/peers/{peer_id}')

    @connection_established
    async def get_peer_by_name(self, name) -> Dict[str, Any]:
        """
        Return peer settings by it's name identifier
        :param name: name of the peer to return
        """
        peers = await self._get(f'/peers?name={name}')
        if len(peers) == 0:
            raise NETunnelError(f'Peer `{name}` was not found')
        # peer name is unique
        return peers[0]

    @connection_established
    async def list_peer_static_tunnels(self, peer_name):
        """
        Return a list of static tunnels to a peer
        :param peer_name: name of the peer on which the static tunnels are headed
        """
        peer = await self.get_peer_by_name(peer_name)
        peer_id = peer['id']
        return await self._get(f'/peers/{peer_id}/static_tunnels')

    @connection_established
    async def create_peer_static_tunnel(self, peer_name, tunnel_remote_port, tunnel_remote_address='127.0.0.1'):
        """
        Creates a static tunnel to a peer
        :param peer_name: name of the peer on which to create the static tunnel
        :param tunnel_remote_port: exit port of the tunnel on the peer
        :param tunnel_remote_address: exit address of the tunnel on the peer
        """
        peer = await self.get_peer_by_name(peer_name)
        peer_id = peer['id']
        static_tunnel_payload = {
            'tunnel_remote_address': tunnel_remote_address,
            'tunnel_remote_port': tunnel_remote_port
        }
        return await self._post(f'/peers/{peer_id}/static_tunnels', json=static_tunnel_payload)

    @connection_established
    async def get_peer_static_tunnel(self, peer_name, static_tunnel_id):
        """
        Return a static tunnel settings of a peer
        :param peer_name: name of the peer on which the static tunnel is set
        :param static_tunnel_id: id of the static tunnel on the peer to return
        """
        peer = await self.get_peer_by_name(peer_name)
        peer_id = peer['id']
        return await self._get(f'/peers/{peer_id}/static_tunnels/{static_tunnel_id}')

    @connection_established
    async def delete_peer_static_tunnel(self, peer_name, static_tunnel_id):
        """
        Delete a static tunnel of a peer
        :param peer_name: name of the peer on which the static tunnel is set
        :param static_tunnel_id: id of the static tunnel on the peer to delete
        """
        peer = await self.get_peer_by_name(peer_name)
        peer_id = peer['id']
        return await self._delete(f'/peers/{peer_id}/static_tunnels/{static_tunnel_id}')

    @connection_established
    async def get_server_default_http_proxy(self):
        """
        Return the current http proxy settings on the remote netunnel server
        """
        return await self._get('/config/http-proxy')

    @connection_established
    async def set_server_default_http_proxy(self, proxy_url, username=None, password=None, test_url=None, check_proxy=True):
        """
        Set the http proxy settings on the remote netunnel server
        :param proxy_url: HTTP proxy url to set
        :param username: Optional username to authenticate with the proxy. `password` must be given as well
        :param password: Optional password to authenticate with the proxy. `username` must be given as well
        :param test_url: A url that will be used to verify the proxy_url by making a GET request to it via the proxy
        :param check_proxy: Whether to validate that the proxy works. If set to False, the test_url will be ignored
        """
        http_proxy_payload = {
            'proxy_url': proxy_url,
            'username': username,
            'password': password,
            'test_url': test_url,
            'check_proxy': check_proxy
        }
        return await self._post('/config/http-proxy', json=http_proxy_payload)

    async def __aenter__(self):
        try:
            await self.connect()
        except Exception:
            await self._close_session()
            raise
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()


def get_args():
    parser = argparse.ArgumentParser(description='Run a netunnel client')
    parser.add_argument('-d', '--debug', help="Increase log verbosity to debug mode", dest='loglevel', action="store_const",
                        const=logging.DEBUG, default=logging.INFO)
    parser.add_argument('-s', '--server', help="Address of the NETunnel Server to connect", default='http://127.0.0.1:4040')
    parser.add_argument('--local-address', help="Local address to listen/connect to", default='127.0.0.1')
    parser.add_argument('--local-port', help="Local port to listen/connect to. Defaults to random. Mandatory for reverse tunnel", type=int)
    parser.add_argument('--remote-address', help="Remote address to listen/connect to", default='127.0.0.1')
    parser.add_argument('--remote-port', help="Remote port to listen/connect to", type=int, default=22)
    parser.add_argument('-r', '--reverse', action="store_true", help="Reverse the tunnel. The remote socket will be the entrance while the local socket will be the exit")
    parser.add_argument('--auth-plugin', help="Plugin to use for authentication. (e.g. <module_path>.<auth_class_name>)")
    parser.add_argument('--auth-data', default='{}', help="A json dump string of the data required by the authentication plugin")
    parser.add_argument('--no-ssl-validate', action="store_true", help="Do not validate the certificate of the server")
    parser.add_argument('--proxy-url', help="URL to a proxy server between the client and the server")
    parser.add_argument('--proxy-username', help="Optional username to use to authenticate with the proxy")
    parser.add_argument('--proxy-password', help="Optional password to use to authenticate with the proxy")
    return parser.parse_args()


async def main():
    args = get_args()
    logger = get_logger('netunnel_client', args.loglevel)
    auth_client = None
    if args.auth_plugin:
        module, class_name = args.auth_plugin.rsplit('.', maxsplit=1)
        auth_class = getattr(importlib.import_module(module), class_name)
        auth_client = auth_class(**json.loads(args.auth_data))
    ssl = None
    if args.no_ssl_validate:
        ssl = False
    async with NETunnelClient(server_url=args.server, proxy_url=args.proxy_url, proxy_username=args.proxy_username,
                              proxy_password=args.proxy_password, logger=logger, auth_client=auth_client, ssl=ssl) as client:
        if args.reverse:
            if args.local_port is None:
                raise ValueError("--local-port is required for reverse tunnel")
            print("Opening tunnel to the client...")
            tunnel = await client.open_tunnel_to_client(local_address=args.local_address,
                                                        local_port=args.local_port,
                                                        remote_address=args.remote_address,
                                                        remote_port=args.remote_port)

        else:
            print("Opening tunnel to the server...")
            tunnel = await client.open_tunnel_to_server(local_address=args.local_address,
                                                        local_port=args.local_port,
                                                        remote_address=args.remote_address,
                                                        remote_port=args.remote_port)
        print("Tunnel entrance socket: %s:%d" % tunnel.get_entrance_socket())
        print("Tunnel exit socket: %s:%d" % tunnel.get_exit_socket())
        await tunnel.join()


if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        # KeyboardInterrupt raises outside the event loop (and pausing it) so we cancel the tasks and let them close cleanly
        tasks = asyncio_all_tasks(loop)
        for task in tasks:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            loop.run_until_complete(asyncio.gather(*tasks))
