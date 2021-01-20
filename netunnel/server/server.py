from typing import Dict, Any

from aiohttp import web
from colorama import Fore, Style

from .. import __version__
from ..common import utils, const, security, exceptions, auth
from ..common.exceptions import NETunnelDestinationNotAllowed, NETunnelInvalidProxy
from .schemas import StaticTunnelSchema, PeerSchema
from .peer import Peer
from .config import NETunnelConfiguration
from .client_handler import ChannelHandler

import os
import json
import http
import logging
import asyncio
import argparse
import importlib
import functools
import contextlib


SECRET_STRING = "******"


def exceptions_to_json_response(async_route_method):
    """
    Convert exceptions to json error responses.
    http status codes can be bind to an exception class. Every unknown exception is considered internal_server_error (500)
    See netunnel.common.exceptions.NETunnelServerError
    :param async_route_method: A route method which accepts aiohttp aiohttp.web.Request object
    """
    @functools.wraps(async_route_method)
    async def wrap(self: 'NETunnelServer', request: web.Request, *args, **kwargs):
        try:
            return await async_route_method(self, request, *args, **kwargs)
        except exceptions.NETunnelServerError as err:
            return self._error_json(str(err), status=err.status_code.value)
        except Exception as err:
            self._logger.exception('The following exception raised for route `%s`:', request.rel_url)
            return self._error_json(str(err), status=http.HTTPStatus.INTERNAL_SERVER_ERROR.value)
    return wrap


def authenticated(async_route_method):
    """
    Validate that a request is authorized. Return Forbidden if not
    :param async_route_method: A route method which accepts aiohttp aiohttp.web.Request object
    """
    @functools.wraps(async_route_method)
    async def wrap(self: 'NETunnelServer', request: web.Request, *args, **kwargs):
        if await self._auth_server.is_authenticated(request):
            return await async_route_method(self, request, *args, **kwargs)
        self._logger.debug('Unauthorized request made to the route `%s`', request.rel_url)
        return web.HTTPForbidden()
    return wrap


class NETunnelServer:
    def __init__(self, config_path=None, host='127.0.0.1', port=4040, logger=None,
                 auth_server: auth.NETunnelServerAuth = None, secret_key=None):
        """
        NETunnel server. Listen on host:port and serve clients with tunnels.
        :param config_path: Path to the configuration file. If None, the server won't keep state
        :param host: Address to listen on
        :param port: Port to listen on
        :param logger: Optional logger
        :param auth_server: Instance of subclass of netunnel.common.auth.NETunnelServerAuth that will be used for authentication
        :param secret_key: A secret_key to use instead of generating and storing one in the configuration file
        """
        self._config_path = config_path
        self._host = host
        self._port = port
        self._logger = logger or utils.get_logger(__name__)
        self.channel_handlers: Dict[const.ChannelId, ChannelHandler] = {}
        self._channel_id_counter = 0
        # This lock is used to prevent duplications of channel_ids when multiple requests received at once
        self._channel_handlers_lock = asyncio.Lock()
        self._app = web.Application()
        self._runner: web.AppRunner = None
        self._auth_server: auth.NETunnelServerAuth = auth_server or auth.ServerNoAuth()
        self._setup_routes()
        self._config: NETunnelConfiguration = None
        # mapping of peer_id to Peer object
        self._peers: Dict[int, Peer] = {}
        # This lock is used when registering new peer to prevent id duplications
        self._registering_peer_lock = asyncio.Lock()
        # Used to prevent static tunnel's local port duplications on creation
        self._creating_static_tunnel_lock = asyncio.Lock()
        # This is used to encrypt and decrypt sensitive information
        self._secret_key = secret_key
        self._encryptor: security.Encryptor = None
        self._peer_schema = PeerSchema()
        self._static_tunnel_schema = StaticTunnelSchema()

    def _setup_routes(self):
        self._app.add_routes([
            web.get('/version', self.get_version),
            web.post('/channels', self.create_channel),  # Creates a channel
            web.post('/authenticate', self._auth_server.authenticate),
            web.get('/channels/{channel_id:\d+}/connect', self.serve_channel),  # Connect the channel (websocket)
            web.post('/channels/{channel_id:\d+}/tunnels', self.post_tunnel),  # Creates a tunnel
            web.delete('/channels/{channel_id:\d+}/tunnels/{tunnel_id:\d+}', self.delete_tunnel),  # Delete a tunnel
            web.get('/channels/{channel_id:\d+}/tunnels/{tunnel_id:\d+}/connect', self.websocket_to_tunnel),  # Feed a tunnel with a websocket
            web.get('/peers', self.list_peers),
            web.post('/peers', self.register_peer),
            web.get('/peers/{peer_id:\d+}', self.get_peer),
            web.post('/peers/{peer_id:\d+}', self.update_peer),
            web.delete('/peers/{peer_id:\d+}', self.delete_peer),
            web.get('/peers/{peer_id:\d+}/static_tunnels', self.list_peer_static_tunnels),
            web.post('/peers/{peer_id:\d+}/static_tunnels', self.create_peer_static_tunnels),
            web.get('/peers/{peer_id:\d+}/static_tunnels/{static_tunnel_id:\d+}', self.get_peer_static_tunnel),
            web.delete('/peers/{peer_id:\d+}/static_tunnels/{static_tunnel_id:\d+}', self.delete_peer_static_tunnel),
            web.get('/config/http-proxy', self.get_default_http_proxy),  # Return the default http proxy settings
            web.post('/config/http-proxy', self.set_default_http_proxy)  # Set the default http proxy
        ])

    def _get_decrypted_proxy_settings(self):
        """
        Return a tuple of the global proxy settings in the format (url, username, password)
        after decrypting the username / password if necessary
        """
        http_proxy_config: dict = self._config.get('http_proxy') or {}
        proxy_url = http_proxy_config.get('proxy_url')
        proxy_username = http_proxy_config.get('username')
        proxy_password = http_proxy_config.get('password')
        proxy_username = proxy_username and self._encryptor.decrypt_string(proxy_username)
        proxy_password = proxy_password and self._encryptor.decrypt_string(proxy_password)
        return proxy_url, proxy_username, proxy_password

    async def _setup_config(self):
        """
        Setup NETunnel configuration.
        If a configuration path was given, generate configuration from it
        """
        self._config = await NETunnelConfiguration.create(self._config_path)

    async def _setup_peers(self):
        """
        Setup peers and static tunnels that are stored in the configuration file.
        We do not need to verify them, as we trust that even if they aren't available right now, they will be
        """
        for peer_config in self._config['peers']:
            self._logger.info('Setup peer `%s`', peer_config['name'])
            peer = await self._load_peer_from_config(peer_config, verify_connectivity=False)
            self._peers[peer.id] = peer

    async def _setup_encryptor(self):
        """
        Initialize the secret key which will be used to encrypt and decrypt sensitive data.
        ** If the key was provided during initializing, we keep it only in memory **
        """
        if self._secret_key:
            self._encryptor = security.Encryptor(self._secret_key)
        elif 'NETUNNEL_SECRET_KEY' in os.environ:
            self._encryptor = security.Encryptor(os.environ['NETUNNEL_SECRET_KEY'])
        elif 'secret_key' in self._config:
            self._encryptor = security.Encryptor(self._config['secret_key'])
        else:
            self._logger.info('Generating default secret-key')
            self._secret_key = security.Encryptor.generate_key().decode()
            self._encryptor = security.Encryptor(self._secret_key)
            self._config['secret_key'] = self._secret_key
            await self._config.save()

    async def _start_web_server(self):
        self._logger.info('Starting server on %s:%s', self._host, self._port)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()

    async def _stop_web_server(self):
        if self._runner is not None:
            await self._runner.cleanup()

    async def _load_peer_from_config(self, peer_config, verify_connectivity=True) -> Peer:
        """
        Load a peer from a dictionary
        :param peer_config: settings of the peer to load in the format of dict
        :param verify_connectivity: Whether to verify connectivity to the peer
        """
        proxy_url, proxy_username, proxy_password = self._get_decrypted_proxy_settings()
        peer_load = self._peer_schema.load3(peer_config)
        # Once marshmallow supports asyncio in @post_load decorators, consider moving it there
        try:
            peer_load['auth'] = await self._auth_server.get_client_for_peer(**peer_load.pop('auth_data'))
        except (TypeError, ValueError) as err:
            raise exceptions.NETunnelServerBadRequest(str(err))
        # We strip the static_tunnels as they are initialized separately
        static_tunnels = peer_load.pop('static_tunnels', [])
        ssl = False if self._config['allow_unverified_ssl_peers'] else None
        peer = Peer(**peer_load, proxy_url=proxy_url, proxy_username=proxy_username, proxy_password=proxy_password,
                    ssl=ssl, logger=self._logger)
        if verify_connectivity:
            await peer.verify_connectivity()
        for static_tunnel in static_tunnels:
            await peer.add_static_tunnel(**static_tunnel, verify_connectivity=verify_connectivity)
        return peer

    @staticmethod
    def _error_json(err_message: str, data: Any = None, status=500, **json_response_kwargs):
        response = {
            'error': err_message
        }
        if data is not None:
            response['data'] = data
        return web.json_response(response, status=status, **json_response_kwargs)

    def _get_peer_by_id(self, peer_id) -> Peer:
        """
        Return peer object by peer_id. If peer not exists, raises NETunnelServerNotFound
        """
        if peer_id not in self._peers:
            raise exceptions.NETunnelServerNotFound(f'Peer `{peer_id}` was not found')
        return self._peers[peer_id]

    def _get_peer_by_name(self, peer_name) -> Peer:
        """
        Return peer object by name. If peer not exists, raises NETunnelServerNotFound
        :param peer_name: name of the peer to return
        """
        for peer in self._peers.values():
            if peer_name == peer.name:
                return peer
        raise exceptions.NETunnelServerNotFound(f'Peer `{peer_name}` was not found')

    def _get_peer_config_by_id(self, peer_id):
        """
        Return the config object of the peer by id. If peer not exists, raises NETunnelServerNotFound
        """
        for peer in self._config['peers']:
            if peer['id'] == peer_id:
                return peer
        raise exceptions.NETunnelServerNotFound(f'Peer `{peer_id}` was not found in the config')

    def _get_used_static_tunnels_local_ports(self):
        """
        Return a list of all the used local ports by static tunnels from all the peers.
        """
        used_ports = []
        for peer in self._peers.values():
            for tunnel in peer.static_tunnels:
                used_ports.append(tunnel.tunnel_local_port)
        return used_ports

    async def start(self):
        """
        Start netunnel server on host:port and start all static tunnels.
        """
        await self._setup_config()
        await self._setup_encryptor()
        await self._start_web_server()
        await self._setup_peers()

    async def stop(self):
        while self._peers:
            _, peer = self._peers.popitem()
            self._logger.info('Disconnect peer `%s`', peer.name)
            await peer.delete_static_tunnels()
        # We convert to list because the items on self.channel_handlers are being removed after each loop
        for channel_id, channel_handler in list(self.channel_handlers.items()):
            self._logger.debug('Closing client handler `%s`', channel_id)
            await channel_handler.close()
        self._logger.info('Stopping web application')
        await self._stop_web_server()

    @authenticated
    async def get_version(self, request: web.Request):
        """
        Return the running NETunnel version
        """
        return web.Response(text=__version__)

    @authenticated
    @exceptions_to_json_response
    async def create_channel(self, request: web.Request):
        """
        Creates a ChannelHandler object and return the channel_id to the client
        """
        data = await request.json()
        client_version = data['version']
        async with self._channel_handlers_lock:
            self._channel_id_counter += 1
            channel_id = self._channel_id_counter
        self.channel_handlers[channel_id] = ChannelHandler(channel_id=channel_id, client_version=client_version, logger=self._logger)
        # We send the version to support backward compatibility if needed
        return web.json_response({'channel_id': channel_id, 'version': __version__})

    @authenticated
    async def serve_channel(self, request: web.Request):
        """
        Creates a websocket to a channel and start serving it.
        Once we finish serving the channel, we delete it.
        """
        channel_id = int(request.match_info['channel_id'])
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await self.channel_handlers[channel_id].serve(ws)
        del self.channel_handlers[channel_id]
        return ws

    def _verify_tunnel_destination_is_allowed(self, exit_address: str, exit_port: int):
        """
        This function purpose is to make sure a tunnel is not going to access a non-approved destination in the server network.
        It should be in used whenever the server opens a tunnel that its exit is on the server.
        The allowed destinations are set in the configuration under 'allowed_tunnel_destinations', and expected to be
        in the format of {"ip": "port1,port2,port3"}. "*" may be used to allow all ports.
        """
        allowed_tunnel_destinations = self._config.get('allowed_tunnel_destinations')
        if allowed_tunnel_destinations is None:
            raise RuntimeError("NETunnel configurations is missing 'allowed_tunnel_destinations'")
        if exit_address not in allowed_tunnel_destinations:
            raise NETunnelDestinationNotAllowed(f'Address {exit_address} is not allowed as a tunnel destination')
        allowed_ports_for_address = [port.strip() for port in allowed_tunnel_destinations[exit_address].split(',')]
        if str(exit_port) not in allowed_ports_for_address and "*" not in allowed_ports_for_address:
            raise NETunnelDestinationNotAllowed(f'Port {exit_address}:{exit_port} is not allowed as a tunnel destination')

    @authenticated
    @exceptions_to_json_response
    async def post_tunnel(self, request: web.Request):
        """
        Creates a tunnel.
        Each tunnel belong to a ChannelHandler and identified by a tunnel_id.
        return that tunnel_id to the client.
        """
        channel_id = int(request.match_info['channel_id'])
        data = await request.json()
        try:
            if data.pop('reverse'):
                self._logger.info('Opening Server-To-Client tunnel. Listen on: %s:%s', data['entrance_address'], data['entrance_port'])
                tunnel_id = await self.channel_handlers[channel_id].create_server_to_client_tunnel(**data)
            else:
                self._verify_tunnel_destination_is_allowed(data['exit_address'], data['exit_port'])
                self._logger.info('Opening Client-To-Server tunnel. Connecting to: %s:%s', data["exit_address"], data["exit_port"])
                tunnel_id = await self.channel_handlers[channel_id].create_client_to_server_tunnel(**data)
        except (RuntimeError, NETunnelDestinationNotAllowed) as err:
            return self._error_json(str(err))
        return web.json_response({'tunnel_id': tunnel_id})

    @authenticated
    @exceptions_to_json_response
    async def delete_tunnel(self, request: web.Request):
        """
        Delete a tunnel.
        Unlike channels which always have a single alive websocket, tunnels can have many websockets or no websockets
        at a given time, this request is the way for the client to tell the server to delete a tunnel
        """
        channel_id = int(request.match_info['channel_id'])
        tunnel_id = int(request.match_info['tunnel_id'])
        data = await request.json()
        force = data.get('force', False)
        self._logger.debug('Deleting tunnel `%s` on channel `%s`', tunnel_id, channel_id)
        try:
            await self.channel_handlers[channel_id].delete_tunnel(tunnel_id, force)
        except KeyError:
            return self._error_json(f'tunnel_id `{tunnel_id}` does not exists')
        return web.json_response()

    @authenticated
    async def websocket_to_tunnel(self, request: web.Request):
        """
        Generates a new websocket to an existing tunnel
        """
        channel_id = int(request.match_info['channel_id'])
        tunnel_id = int(request.match_info['tunnel_id'])
        self._logger.debug('Serving new websocket for tunnel `%s` on channel `%s`', tunnel_id, channel_id)
        ws = utils.EventWebSocketResponse()
        await ws.prepare(request)
        await self.channel_handlers[channel_id].serve_new_connection(ws, tunnel_id)
        return ws

    @authenticated
    @exceptions_to_json_response
    async def get_default_http_proxy(self, request: web.Request):
        response = self._config.get('http_proxy') or {}
        # Credentials are encrypted
        if response.get('username'):
            response['username'] = SECRET_STRING
            response['password'] = SECRET_STRING
        return web.json_response(response)

    @authenticated
    @exceptions_to_json_response
    async def set_default_http_proxy(self, request: web.Request):
        """
        Set the default http proxy that will be used when static tunnels are established.
        If there are existing static tunnel, they will be reset to apply the new proxy
        """
        data = await request.json()
        if 'proxy_url' not in data:
            return self._error_json("Missing 'proxy_url' for proxy")
        proxy_url = data['proxy_url']
        # A test url that will be used to verify the proxy
        test_url = data.get('test_url') or self._config.get('http_proxy_test_url')
        username = data.get('username')
        password = data.get('password')
        check_proxy = data.get('check_proxy', True)

        if proxy_url is None:
            self._config['http_proxy'] = None
            self._logger.info('Setting empty default proxy url')
        else:
            if check_proxy:
                try:
                    await utils.verify_proxy(proxy_url=proxy_url, test_url=test_url, username=username, password=password)
                except (ValueError, NETunnelInvalidProxy) as err:
                    self._logger.exception('Failed to set default http proxy:')
                    return self._error_json(str(err))
            self._logger.info('Settings default proxy url to `%s`', proxy_url)
            self._config['http_proxy'] = {
                'proxy_url': proxy_url,
                'username': username and self._encryptor.encrypt_string(username),
                'password': password and self._encryptor.encrypt_string(password)
            }
        await self._config.save()
        for peer in self._peers.values():
            await peer.set_new_proxy(proxy_url=proxy_url, proxy_username=username, proxy_password=password)
        return await self.get_default_http_proxy(request)

    @authenticated
    @exceptions_to_json_response
    async def list_peers(self, request: web.Request):
        """
        Return a list of registered peers
        """
        name_filter = request.rel_url.query.get('name')
        peers_to_return = self._config['peers']
        if name_filter:
            peers_to_return = filter(lambda peer: peer['name'] == name_filter, peers_to_return)
        return web.json_response(list(peers_to_return))

    @authenticated
    @exceptions_to_json_response
    async def get_peer(self, request: web.Request):
        """
        Return a peer by id
        """
        peer_id = int(request.match_info['peer_id'])
        peer = self._get_peer_by_id(peer_id)
        return web.json_response(self._peer_schema.dump3(peer))

    @authenticated
    @exceptions_to_json_response
    async def register_peer(self, request: web.Request):
        """
        Register a new peer
        """
        data = await request.json()
        # Verify data
        errors = self._peer_schema.validate(data)
        if errors:
            return self._error_json(f'Failed to register peer: {errors}', status=http.HTTPStatus.BAD_REQUEST.value)

        # Verify unique peer name
        new_peer_name = data['name']
        with contextlib.suppress(exceptions.NETunnelServerNotFound):
            if self._get_peer_by_name(new_peer_name):
                return self._error_json(f"Peer `{new_peer_name}` already exists", status=http.HTTPStatus.BAD_REQUEST.value)

        async with self._registering_peer_lock:
            # Generate an id for the peer
            data['id'] = max(self._peers.keys()) + 1 if self._peers else 1
            new_peer = await self._load_peer_from_config(data)

            # Save new peer
            self._peers[new_peer.id] = new_peer
            new_peer_config = self._peer_schema.dump3(new_peer)
            self._config['peers'].append(new_peer_config)
        await self._config.save()
        return web.json_response(new_peer_config)

    @authenticated
    @exceptions_to_json_response
    async def update_peer(self, request: web.Request):
        """
        Update peer data
        """
        peer_id = int(request.match_info['peer_id'])
        peer = self._get_peer_by_id(peer_id)
        peer_config: dict = self._get_peer_config_by_id(peer_id)
        data = await request.json()

        # We load only the updatable fields
        changeable_fields = ('name', 'target_netunnel_url', 'auth_data')
        partial_schema = PeerSchema(only=changeable_fields)
        fields_to_update = partial_schema.load3(data, partial=changeable_fields)
        if 'auth_data' in fields_to_update:
            # Once marshmallow supports asyncio in @post_load decorators, consider moving it there
            try:
                fields_to_update['auth'] = await self._auth_server.get_client_for_peer(**fields_to_update.pop('auth_data'))
            except (TypeError, ValueError) as err:
                raise exceptions.NETunnelServerBadRequest(str(err))

        # Validate unique name.
        with contextlib.suppress(exceptions.NETunnelServerNotFound):
            # This will raise and ignored if no peer with this name exists
            existing_peer = self._get_peer_by_name(fields_to_update.get('name'))
            # We allow to 'update' the name to the same name since its the expected behavior for POST requests
            if existing_peer is not peer:
                return self._error_json(f"Peer `{fields_to_update['name']}` already exists", status=http.HTTPStatus.BAD_REQUEST.value)
        if fields_to_update.get('name') in [p.name for p in self._peers.values() if p != peer]:
            return self._error_json(f"Peer `{fields_to_update['name']}` already exists")
        # Validate connectivity by creating a temporary peer
        if 'target_netunnel_url' in fields_to_update or 'auth' in fields_to_update:
            proxy_url, proxy_username, proxy_password = self._get_decrypted_proxy_settings()
            # We use the new authentication data or/and new url so we'll know if they're valid before updating the real peer
            url = fields_to_update.get('target_netunnel_url') or peer.target_netunnel_url
            auth = fields_to_update.get('auth') or peer.auth
            ssl = False if self._config['allow_unverified_ssl_peers'] else None
            temp_peer = Peer(id=-1, name='temp', target_netunnel_url=url, auth=auth,
                             proxy_url=proxy_url, proxy_username=proxy_username, proxy_password=proxy_password,
                             ssl=ssl, logger=self._logger)
            await temp_peer.verify_connectivity()

        # Modify the peer object
        if 'target_netunnel_url' in fields_to_update or 'auth' in fields_to_update:
            # This will trigger the static tunnels to be recreated
            await peer.update_settings(new_url=fields_to_update.get('target_netunnel_url'), new_auth=fields_to_update.get('auth'))
        if 'name' in fields_to_update:
            new_name = fields_to_update['name']
            self._logger.info('Renaming peer `%s` to `%s`', peer.name, new_name)
            peer.name = new_name
        peer_config.update(self._peer_schema.dump3(peer))
        await self._config.save()
        return web.json_response(peer_config)

    @authenticated
    @exceptions_to_json_response
    async def delete_peer(self, request: web.Request):
        """
        Deletes a peer by an id
        """
        peer_id = int(request.match_info['peer_id'])
        peer = self._get_peer_by_id(peer_id)
        peer_config = self._get_peer_config_by_id(peer_id)
        await peer.delete_static_tunnels()
        self._config['peers'].remove(peer_config)
        await self._config.save()
        del self._peers[peer.id]
        return web.json_response()

    @authenticated
    @exceptions_to_json_response
    async def list_peer_static_tunnels(self, request: web.Request):
        """
        Return a list of static tunnels of a peer
        """
        peer_id = int(request.match_info['peer_id'])
        peer = self._get_peer_by_id(peer_id)
        return web.json_response(self._static_tunnel_schema.dump3(peer.static_tunnels, many=True))

    @authenticated
    @exceptions_to_json_response
    async def create_peer_static_tunnels(self, request: web.Request):
        """
        Creates a static tunnel to peer
        """
        peer_id = int(request.match_info['peer_id'])
        peer = self._get_peer_by_id(peer_id)
        data: dict = await request.json()
        async with self._creating_static_tunnel_lock:
            # Validate data
            if 'tunnel_local_port' in data:
                return self._error_json("Tunnel's local_port is generated by the netunnel server")
            elif 'id' in data:
                return self._error_json("Tunnel's id is generated by the netunnel server")
            data['tunnel_local_port'] = utils.get_unused_port(min_port=const.MIN_STATIC_TUNNEL_LOCAL_PORT,
                                                              max_port=const.MAX_STATIC_TUNNEL_LOCAL_PORT,
                                                              exclude_ports=self._get_used_static_tunnels_local_ports())
            errors = self._static_tunnel_schema.validate(data)
            if errors:
                return self._error_json(f'Failed to create static tunnel for peer `{peer.name}`: {errors}', status=http.HTTPStatus.BAD_REQUEST.value)
            # We're using the schema's load to generate the default values and emit unnecessary parameters
            static_tunnel = await peer.add_static_tunnel(**self._static_tunnel_schema.load3(data))
            # We're dumping from the object so we'll have auto-generated fields (ID for example) as well
            static_tunnel_settings = self._static_tunnel_schema.dump3(static_tunnel)
            peer_config = self._get_peer_config_by_id(peer_id)
            peer_config['static_tunnels'].append(static_tunnel_settings)
            await self._config.save()
        return web.json_response(static_tunnel_settings)

    @authenticated
    @exceptions_to_json_response
    async def get_peer_static_tunnel(self, request: web.Request):
        """
        Return a static tunnel of a peer by an id
        """
        peer_id = int(request.match_info['peer_id'])
        static_tunnel_id = int(request.match_info['static_tunnel_id'])
        peer = self._get_peer_by_id(peer_id)
        static_tunnel = peer.get_static_tunnel(static_tunnel_id)
        return web.json_response(self._static_tunnel_schema.dump3(static_tunnel))

    @authenticated
    @exceptions_to_json_response
    async def delete_peer_static_tunnel(self, request: web.Request):
        """
        Delete a static tunnel of a peer by an id
        :param request:
        :return:
        """
        peer_id = int(request.match_info['peer_id'])
        static_tunnel_id = int(request.match_info['static_tunnel_id'])
        peer = self._get_peer_by_id(peer_id)
        static_tunnel = peer.get_static_tunnel(static_tunnel_id)
        peer_config = self._get_peer_config_by_id(peer_id)
        static_tunnel_config = self._static_tunnel_schema.dump3(static_tunnel)
        await peer.delete_static_tunnel(static_tunnel_id)
        peer_config['static_tunnels'].remove(static_tunnel_config)
        await self._config.save()
        return web.json_response()


def main():
    parser = argparse.ArgumentParser(description='Run a netunnel server')
    parser.add_argument('-c', '--config-path', help='Configuration file path to use or generate if missing. Defaults to stateless mode')
    parser.add_argument('-d', '--debug', help="increase log verbosity to debug mode", dest='loglevel', action="store_const",
                        const=logging.DEBUG, default=logging.INFO)
    parser.add_argument('-p', '--port', type=int, help="Port number to listen on", default=4040)
    parser.add_argument('-s', '--secret-key',
                        help="A passphrase used for encryption which will not be stored in the configuration file")
    parser.add_argument('--hostname', help="IP address to listen on", default='127.0.0.1')
    parser.add_argument('--auth-plugin', help="Plugin to use for authentication. (e.g. <module_path>.<auth_class_name>)")
    parser.add_argument('--auth-data', default='{}', help="A json dump string of the data required by the authentication plugin")
    args = parser.parse_args()
    logger = utils.get_logger(name='netunnel_server', level=args.loglevel)
    auth_server = None
    if args.auth_plugin:
        module, class_name = args.auth_plugin.rsplit('.', maxsplit=1)
        auth_server_class = getattr(importlib.import_module(module), class_name)
        auth_server = auth_server_class(**json.loads(args.auth_data))
    if args.config_path is None:
        print(f'{Fore.YELLOW}The server is running in stateless mode. Use --config-path to generate a config file{Style.RESET_ALL}')
    server = NETunnelServer(config_path=args.config_path, logger=logger, port=args.port, auth_server=auth_server,
                            secret_key=args.secret_key)
    loop = asyncio.get_event_loop()
    try:
        asyncio.ensure_future(server.start())
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
