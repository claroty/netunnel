from typing import Dict
from aiohttp.web import WebSocketResponse
from ..common.const import TunnelId
from ..common.tunnel import Tunnel, OutputTunnel, InputTunnel
from ..common.utils import EventWebSocketResponse
from ..common.channel import Channel, Messages, ChannelMessage

import asyncio
import logging


class ChannelHandler:
    def __init__(self, channel_id, client_version, logger: logging.Logger):
        """
        Manage a channel to the server.
        :param channel_id: the channel_id to set the created channel
        """
        self._control_channel: Channel = None
        # For backwards compatibility if needed
        self._client_version = client_version
        self._channel_id = channel_id
        self._tunnels: Dict[TunnelId, Tunnel] = {}
        self._tunnel_id_counter = 0
        # This lock is used to prevent duplications of tunnel_ids when multiple requests received at once
        self._tunnels_id_lock = asyncio.Lock()
        self._logger = logger
        # This is used to stop creating new tunnels during shutdown until complete close
        self._closing = False

    async def _get_next_tunnel_id(self):
        async with self._tunnels_id_lock:
            self._tunnel_id_counter += 1
            return self._tunnel_id_counter

    @property
    def running(self):
        return self._control_channel is not None and self._control_channel.running

    async def serve(self, websocket: WebSocketResponse):
        """
        Start serving the channel.
        Close all leftover tunnels after the channel is close
        """
        if self.running:
            raise RuntimeError('Channel is already running')
        self._closing = False
        self._control_channel = Channel(websocket=websocket, channel_id=self._channel_id, logger=self._logger)
        self._logger.debug('Starting serving channel `%s`. Client version: `%s`', self._control_channel.id, self._client_version)
        try:
            await self._control_channel.serve()
        except asyncio.CancelledError:
            self._logger.warning('Channel `%s` was cancelled, probably due to client sudden disconnection', self._control_channel.id)
        except Exception:
            self._logger.exception('Channel `%s` failed to serve:', self._control_channel.id)
            raise
        finally:
            self._logger.debug('Closing channel `%s`', self._control_channel.id)
            for tunnel in list(self._tunnels.values()):
                # At this point, the channel is closed anyway so we can't stop the remote tunnels
                await tunnel.stop(stop_remote_tunnel=False)

    async def close(self, force=True):
        """
        Close the client handler
        :param force: Whether to forcefully close the connections to the tunnels
        """
        self._closing = True
        for tunnel_id, tunnel in list(self._tunnels.items()):
            self._logger.debug('Stopping tunnel `%s` due to channel `%s` shutdown', tunnel_id, self._control_channel.id)
            await tunnel.stop(force=force)
        if self.running:
            await self._control_channel.close()

    async def create_client_to_server_tunnel(self, exit_address, exit_port) -> int:
        """
        Creates a client-to-server tunnel and return the generated tunnel_id
        """
        if self._closing:
            raise RuntimeError('Cannot create new tunnel during shutdown')
        tunnel_id = await self._get_next_tunnel_id()
        self._logger.debug('Creating Client-To-Server Tunnel `%s` for channel `%s`', tunnel_id, self._control_channel.id)
        self._tunnels[tunnel_id] = OutputTunnel(exit_address=exit_address, exit_port=exit_port,
                                                logger=self._logger, stop_tunnel_on_remote_callback=lambda force: self._delete_tunnel_on_client(tunnel_id, force),
                                                stopped_callback=lambda: self._delete_tunnel_from_tunnels(tunnel_id))
        await self._tunnels[tunnel_id].start()
        return tunnel_id

    async def create_server_to_client_tunnel(self, entrance_address, entrance_port):
        """
        Creates a server-to-client tunnel and return the generated tunnel_id
        """
        if self._closing:
            raise RuntimeError('Cannot create new tunnel during shutdown')
        tunnel_id = await self._get_next_tunnel_id()
        self._logger.debug('Creating Server-To-Client Tunnel `%s` for channel `%s`', tunnel_id, self._control_channel.id)
        self._tunnels[tunnel_id] = InputTunnel(entrance_address=entrance_address, entrance_port=entrance_port,
                                               websocket_feeder_coro=self._input_tunnel_websocket_feeder(tunnel_id),
                                               logger=self._logger, stop_tunnel_on_remote_callback=lambda force: self._delete_tunnel_on_client(tunnel_id, force),
                                               stopped_callback=lambda: self._delete_tunnel_from_tunnels(tunnel_id))
        await self._tunnels[tunnel_id].start()
        return tunnel_id

    async def delete_tunnel(self, tunnel_id, force):
        """
        Deletes a tunnel. This is designed to be called as a request from the client, so we will only
        stop the tunnel on our side.
        :param tunnel_id: ID of the tunnel to delete
        :param force: Whether to call stop tunnel forcefully or wait for connections to finish
        """
        try:
            await self._tunnels[tunnel_id].stop(stop_remote_tunnel=False, force=force)
        except KeyError:
            raise KeyError(f'Tunnel id `{tunnel_id}` was not found on channel `{self._channel_id}`')

    async def _delete_tunnel_on_client(self, tunnel_id, force):
        """
        Request the client to delete it's tunnel via the control channel
        :param tunnel_id: ID of the tunnel to delete
        :param force: Whether to call stop tunnel forcefully or wait for connections to finish
        """
        self._logger.debug('Requesting client to stop tunnel `%s` on channel `%s`', tunnel_id, self._channel_id)
        delete_tunnel_message = ChannelMessage(message_type=Messages.DELETE_TUNNEL, data={'tunnel_id': tunnel_id, 'force': force})
        await self._control_channel.send_message(delete_tunnel_message, raise_if_error=True)

    async def _delete_tunnel_from_tunnels(self, tunnel_id):
        self._logger.debug('Closing tunnel `%s` on channel `%s`', tunnel_id, self._channel_id)
        del self._tunnels[tunnel_id]

    async def _input_tunnel_websocket_feeder(self, tunnel_id):
        """
        Get the tunnel_id of an InputTunnel and as long as this tunnel running, wait until it consumes it's pending websocket
        and then requests the client for a new one.
        """
        tunnel = self._tunnels[tunnel_id]
        while tunnel.running:
            await tunnel.wait_no_websocket()
            if not tunnel.running:
                break
            self._logger.debug('Generating new websocket for tunnel `%s` on channel `%s`', tunnel_id, self._control_channel.id)
            new_connection_message = ChannelMessage(message_type=Messages.TUNNEL_SERVER_TO_CLIENT_NEW_CONNECTION, data={'tunnel_id': tunnel_id})
            response = await self._control_channel.send_message(new_connection_message)
            if not response.is_ok():
                self._logger.error('Failed to request the client to create a new websocket for tunnel_id `%s`: %s.', tunnel_id, response.error)
            try:
                await asyncio.wait_for(tunnel.wait_new_websocket(), timeout=10)
            except asyncio.TimeoutError:
                self._logger.warning('Failed to retrieve websocket for Channel `%s` on Tunnel `%s` after 10 seconds. Retrying..', self._channel_id, tunnel_id)

    async def serve_new_connection(self, websocket: EventWebSocketResponse, tunnel_id: int):
        """
        Get a websocket and feed it to the tunnel by the given id.
        wait until the tunnel finished with the websocket before closing it
        """
        if not self.running:
            raise RuntimeError('Channel is not connected')
        if tunnel_id not in self._tunnels:
            raise RuntimeError(f'Tunnel by id `{tunnel_id}` does not exists')
        await self._tunnels[tunnel_id].feed_websocket(websocket)
        await websocket.wait_closed()
