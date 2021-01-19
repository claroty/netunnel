"""
Utilities for creating tunnels on top of websockets
The 2 classes you'll need to use are:
InputTunnel -> Listen on a local address and port. For each connection, it takes a new websocket and forward the data on top of it
OutputTunnel -> Listen on a websocket for incoming messages and forward all data to a designated local address and port

Diagram for how traffic looks like inside the tunnel:

Connection1 ->                  Websocket1 ->               ->
Connection2 ->  InputTunnel ->  Websocket2 ->  OutputTunnel -> Service
Connection3 ->                  Websocket3 ->               ->

traffic goes bi-directional to allow `Service` to respond

For 'reverse tunnel', you just need to create them in reverse.
"""
import asyncio
import logging
import subprocess
import contextlib
import abc
import aiohttp
import aiohttp.web

from typing import List, Coroutine, Callable
from asyncio.base_events import Server
from .const import WebsocketType, CallableOfCoroutine
from .utils import get_logger, EventQueue, object_in_list, task_in_list_until_done, asyncio_current_task

CONNECTION_READ_CHUNK_SIZE = 65535
# This message is used when a connection arrived on InputTunnel to send to the OutputTunnel.
# This is utilized by OutputTunnel to avoid establishing connections with websockets that aren't
# needed yet by InputTunnel and just exists to reduce latency for new connections.
WEBSOCKET_CONNECTION_START_MESSAGE = 'pasten'


class _ConnectionHandler:
    def __init__(self, websocket: WebsocketType, connection_reader: asyncio.StreamReader, connection_writer: asyncio.StreamWriter, logger: logging.Logger):
        """
        Manage a connection. This is used internally, do not initialize this.
        Used by both InputTunnel for new connections to the tunnel, and OutputTunnel for connections established to a service.
        Create read and write pipes between the websocket and the connection:  websocket <-> connection
        :param websocket: websocket to receive and send data in and from the connection
        :param connection_reader: StreamReader object used to read data and send it to the websocket
        :param connection_writer: StreamWriter object used to write data that came from the websocket
        """
        self._websocket = websocket
        self._connection_reader = connection_reader
        self._connection_writer = connection_writer
        self._websocket_to_connection_task: asyncio.Task = None
        self._connection_to_websocket_task: asyncio.Task = None
        self._logger = logger
        # The following event is triggered when either the websocket or the connection closed or reached end-of-file
        self._eof_event = asyncio.Event()
        # Replace with self._connection_writer.wait_closed in Python3.7
        self._shutdown_event = asyncio.Event()

    async def health_check(self):
        """
        Check that both the websocket and the connection can still stream data
        """
        if self._websocket_to_connection_task and self._connection_to_websocket_task:
            return not self._websocket_to_connection_task.done() and not self._connection_to_websocket_task.done() and \
                   not self._websocket.closed and not self._connection_reader.at_eof()
        return False

    async def _websocket_to_connection(self):
        """
        This method stream data that comes from the websocket to the connection_writer.
        If either there is not data left to send (the websocket closed) or there was an error in the communication,
        we signal the self._eof_event.
        """
        try:
            async for msg in self._websocket:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    self._connection_writer.write(msg.data)
                    try:
                        await self._connection_writer.drain()
                    except ConnectionError:
                        self._logger.debug('Connection closed. cannot send data from websocket. Closing websocket-to-connection pipe')
                        break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    self._logger.exception("Websocket disconnected unexpectedly", exc_info=self._websocket.exception())
                    break
                else:
                    self._logger.warning('unexpected message received from server. Type: %s, Data: %s', msg.type.name, msg.data)
        finally:
            self._eof_event.set()

    async def _connection_to_websocket(self):
        """
        This method stream data that comes from the connection_reader to the websocket.
        If there is no more data to read or there was exception in either reading or writing to the websocket,
        we signal the self._eof_event.
        """
        try:
            while not self._connection_reader.at_eof() and not self._websocket.closed:
                data = await self._connection_reader.read(CONNECTION_READ_CHUNK_SIZE)
                try:
                    await self._websocket.send_bytes(data)
                except ConnectionError:
                    self._logger.debug('Websocket closed. cannot send data from connection. Closing connection-to-websocket pipe')
                    break
        finally:
            self._eof_event.set()

    async def _start(self):
        """
        Start read and write pipes
        This should not be used directly, use self.run_until_eof() instead.
        """
        self._shutdown_event.clear()
        self._websocket_to_connection_task = asyncio.ensure_future(self._websocket_to_connection())
        self._connection_to_websocket_task = asyncio.ensure_future(self._connection_to_websocket())

    async def run_until_eof(self):
        """
        Start working and wait until either websocket's closed (remote at eof) or until connection at eof
        """
        await self._start()
        await self._eof_event.wait()
        await self.stop()

    async def stop(self):
        """
        Stop read and write pipes and close the connection
        """
        for task in (self._websocket_to_connection_task, self._connection_to_websocket_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    self._logger.exception('An error occurred while awaiting connection pipe to be closed:')
        self._connection_writer.close()
        self._shutdown_event.set()
    
    async def join(self):
        with contextlib.suppress(asyncio.CancelledError):
            await self._shutdown_event.wait()


class Tunnel(abc.ABC):
    def __init__(self, entrance_address=None, entrance_port=None, exit_address=None, exit_port=None,
                 logger: logging.Logger = None, stop_tunnel_on_remote_callback: Callable[[bool], Coroutine] = None,
                 stopped_callback: CallableOfCoroutine = None):
        """
        This is an interface to manage tunnels.
        A tunnel is an InputTunnel on one side and OutputTunnel on the other side, and reverse tunnel is just the same but
        in reverse. We want the user to have the same interface for when using both normal tunnels and reverse tunnels,
        therefore this class will be used to simplify his usage, and also to share common utilities between InputTunnel and OutputTunnel.
        :param entrance_address: Tunnel's entrance address
        :param entrance_port: Tunnel's entrance port
        :param exit_address: Tunnel's exit address
        :param exit_port: Tunnel's exit port
        :param logger: A logging.Logger object
        :param stop_tunnel_on_remote_callback: callback for when we need to stop the remote tunnel. Accept parameter `force`
        :param stopped_callback: callback for after the tunnel is stopped
        """
        self._entrance_address = entrance_address
        self._entrance_port = entrance_port
        self._exit_address = exit_address
        self._exit_port = exit_port
        self._connections: List[_ConnectionHandler] = []
        self._logger = logger or get_logger(__name__)
        self._stop_tunnel_on_remote_callback = stop_tunnel_on_remote_callback
        self._stopped_callback = stopped_callback
        # This queue is responsible to hold new websockets ready to be used for incoming connections.
        # Each item on the queue is a tuple of (websocket, websocket_done_event):
        # websocket - this websocket is connected to the tunnel's other side
        # websocket_done_event - this event is triggered by the tunnel when the websocket is no needed anymore because eof reached
        # This helps us to separate the tunnel logic from the client/server logic and also reduce latency in creating websockets.
        self._websocket_queue = EventQueue(maxsize=1)
        self._running = False
        self._shutdown = asyncio.Event()

    def get_entrance_socket(self):
        return self._entrance_address, self._entrance_port

    def get_exit_socket(self):
        return self._exit_address, self._exit_port

    @property
    def running(self):
        return self._running

    async def health_check(self):
        """
        Perform a health check to the tunnel.
        Make sure that the tunnel is running and all the existing connections are working.
        see InputTunnel and OutputTunnel for their individual checks as well.
        """
        return self.running and all([await connection.health_check() for connection in self._connections])

    async def start(self):
        self._running = True
        self._shutdown.clear()

    async def stop(self, stop_remote_tunnel=True, force=False):
        """
        Stop the tunnel by closing the connections and the queued websocket.
        We first initiate the queued websocket to be closed in the background so it will start listening for messages,
        and then we ask the remote to also stop so it will start listening to his queued websocket and receive our close message.
        :param stop_remote_tunnel: Whether to call the callback for stopping tunnel on remote. Remote won't be using it to prevent a loop.
        :param force: Whether to close connections forcefully. Otherwise, await for the connections to be closed normally
        """
        if self._running is False:
            return
        self._running = False
        websocket: WebsocketType = None
        try:
            while self._connections:
                    connection = self._connections.pop()
                    if force:
                        await connection.stop()
                    else:
                        await connection.join()
            with contextlib.suppress(asyncio.QueueEmpty):
                websocket = await self._get_websocket(nowait=True)
                websocket.close_nowait()
            if stop_remote_tunnel and self._stop_tunnel_on_remote_callback:
                await self._stop_tunnel_on_remote_callback(force=force)
        except Exception:
            self._logger.exception('An error occurred while stopping tunnel:')
            raise
        finally:
            # stopping tunnel on remote might throw a network related exception but tunnel is still considered close and
            # we still need to await for the yet-to-be-closed websocket to be closed cleanly (probably by timeout if there were an exception)
            try:
                if websocket is not None:
                    await websocket.wait_closed()
            except aiohttp.WebSocketError:
                self._logger.exception('An error occurred while closing an unused websocket:')
            self._shutdown.set()
            if self._stopped_callback:
                await self._stopped_callback()

    async def join(self):
        """
        Blocks until the tunnel is closed
        """
        await self._shutdown.wait()

    async def feed_websocket(self, websocket: WebsocketType):
        """
        Put a websocket in the self._websocket_queue
        """
        if not self.running:
            raise RuntimeError('Tunnel is not running')
        await self._websocket_queue.put(websocket)

    async def wait_new_websocket(self):
        """
        Blocks until there is a new websocket on the queue
        """
        await self._websocket_queue.join_no_empty()

    async def wait_no_websocket(self):
        """
        Blocks until there is no websocket on the queue
        """
        await self._websocket_queue.join()

    async def _get_websocket(self, nowait=False) -> WebsocketType:
        """
        Return an item from self._websocket_queue, and also mark the task as done.
        Marking done is for the client/server to already start working on the next websocket so we can
        reduce latency for new connections.
        :param nowait: Call get_nowait instead of get. This won't block but raise if no websocket on the queue
        """
        if nowait:
            websocket = self._websocket_queue.get_nowait()
        else:
            websocket = await self._websocket_queue.get()
        self._websocket_queue.task_done()
        return websocket


class InputTunnel(Tunnel):
    def __init__(self, entrance_address, entrance_port, websocket_feeder_coro: Coroutine, exit_address=None, exit_port=None,
                 logger: logging.Logger = None, stop_tunnel_on_remote_callback: Callable[[bool], Coroutine] = None, stopped_callback: CallableOfCoroutine = None):
        """
        Creates a local tcp server and listen on _entrance_address and _entrance_port
        For each connection, it creates a websocket to the remote server and start streaming the data on top of it and vice-versa
        This is basically the tunnel's entrance while OutputTunnel is the exit
        :param websocket_feeder_coro: coroutine which responsible to feed this InputTunnel instance with websockets.
                                      it makes the code simpler since InputTunnel can hook it to the start and close methods
        """
        super().__init__(entrance_address, entrance_port, exit_address, exit_port, logger, stop_tunnel_on_remote_callback, stopped_callback)
        self._websocket_feeder_coro: Coroutine = websocket_feeder_coro
        self._websocket_feeder_task: asyncio.Future = None
        # A list of the active tasks which serves connections to self._server. Used for cleanup when stopping the tunnel
        self._active_handle_connection_tasks = []
        self._server: Server = None

    async def health_check(self):
        # According to asyncio doc, Server.sockets is None if the server is closed
        # On python3.7+ replace self._server.sockets with self._server.is_serving()
        return await super().health_check() and self._server.sockets and not self._websocket_feeder_task.done()

    def get_local_socket(self):
        return self.get_entrance_socket()

    def get_remote_socket(self):
        return self.get_exit_socket()

    async def start(self):
        """
        Start the server to listen and handle incoming connections from the local address and port.
        This also starts the websocket_feeder so we can start to receive new websockets
        """
        self._server = await asyncio.start_server(self._handle_connection, host=self._entrance_address, port=self._entrance_port)
        self._entrance_address, self._entrance_port = self._server.sockets[0].getsockname()
        self._logger.debug('Start listening on %s:%s', self._entrance_address, self._entrance_port)
        await super().start()
        self._websocket_feeder_task = asyncio.ensure_future(self._websocket_feeder_coro)

    async def stop(self, stop_remote_tunnel=True, force=False):
        if self._running is False:
            return
        try:
            self._logger.debug('Stop listening on %s:%s', self._entrance_address, self._entrance_port)
            self._server.close()
            await self._server.wait_closed()
            if force:
                self._logger.debug('Stopping any remaining connections on %s:%s', self._entrance_address, self._entrance_port)
                for task in self._active_handle_connection_tasks:
                    task.cancel()
            self._logger.debug('Stopping websocket feeder on %s:%s', self._entrance_address, self._entrance_port)
            self._websocket_feeder_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._websocket_feeder_task
        except Exception:
            self._logger.exception('An error occurred while stopping tunnel entrance handler:')
        finally:
            await super().stop(stop_remote_tunnel=stop_remote_tunnel, force=force)
            # Any exception will be handled or at least logged in the task, yet we await it to satisfy asyncio.
            # We await only after super().stop(...) since we need self.running to be False.
            with contextlib.suppress(Exception):
                await asyncio.gather(*self._active_handle_connection_tasks)

    @staticmethod
    def _get_connection_identity_display_name(connection_writer: asyncio.StreamWriter) -> str:
        """
        Try to query the connection_writer to identify the connection source and return it.
        If we failed to identify the source, we return "Unknown"
        """
        client_process: subprocess.Popen = connection_writer.get_extra_info('subprocess')
        client_peername = connection_writer.get_extra_info('peername')
        if client_process is not None:
            return f'PID `{client_process.pid}`'
        elif client_peername is not None:
            return f'Socket `{client_peername}`'
        return 'Unknown'

    async def _handle_connection(self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter):
        """
        Await a websocket to dedicate it for that connection and a _ConnectionHandler to manage communications between them
        """
        current_task = asyncio_current_task()
        with object_in_list(current_task, self._active_handle_connection_tasks):
            connection_identity_display_name = self._get_connection_identity_display_name(client_writer)
            self._logger.debug('Serving new connection from %s', connection_identity_display_name)
            websocket = None
            try:
                while websocket is None:
                    try:
                        websocket = await asyncio.wait_for(self._get_websocket(), timeout=5)
                        # We notify the remote that this websocket is about to be used
                        await websocket.send_str(WEBSOCKET_CONNECTION_START_MESSAGE)
                    except asyncio.TimeoutError:
                        # We stop waiting for websocket every few seconds to make sure the tunnel still works
                        # and the client is still await.
                        if not self.running or client_writer.transport.is_closing():
                            return
                    except ConnectionError:
                        # This might occurred when a queued websocket has been pending long enough, which means it got closed by
                        # the remote / proxy and we'll know about it only on the first message.
                        # The queue will be filled up with a fresh websocket so we try again.
                        websocket = None
                        continue
                connection = _ConnectionHandler(websocket=websocket, connection_reader=client_reader, connection_writer=client_writer, logger=self._logger)
                with object_in_list(connection, self._connections):
                    self._logger.debug('Start serving %s', connection_identity_display_name)
                    await connection.run_until_eof()
            except asyncio.CancelledError:
                self._logger.debug('Abort handling connection to %s', connection_identity_display_name)
            except Exception:
                self._logger.exception('Failed to serve new connection. Closing connection')
                raise
            finally:
                self._logger.debug('Close connection from %s', connection_identity_display_name)
                client_writer.close()
                if websocket:
                    await websocket.close()


class OutputTunnel(Tunnel):
    def __init__(self, exit_address, exit_port, entrance_address=None, entrance_port=None, logger: logging.Logger = None,
                 stop_tunnel_on_remote_callback: Callable[[bool], Coroutine] = None, stopped_callback: CallableOfCoroutine = None):
        """
        This is basically the tunnel's exit while InputTunnel is the entrance. See Tunnel class doc or this modules
        doc above for further understanding.
        """
        super().__init__(entrance_address, entrance_port, exit_address, exit_port, logger, stop_tunnel_on_remote_callback, stopped_callback)
        self._running_task: asyncio.Task = None
        self._serving_connections_tasks = []

    async def health_check(self):
        return await super().health_check() and not self._running_task.done()

    def get_local_socket(self):
        return self.get_exit_socket()

    def get_remote_socket(self):
        return self.get_entrance_socket()

    async def start(self):
        """
        Start processing new websockets and create connections
        """
        await super().start()
        self._running_task = asyncio.ensure_future(self._run())

    async def stop(self, stop_remote_tunnel=True, force=False):
        """
        Stop receiving new websockets and close all existing connections
        """
        try:
            if self._running_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    self._running_task.cancel()
                    await self._running_task
        except Exception:
            self._logger.exception('An error occurred while stopping tunnel exit handler:')
        finally:
            await super().stop(stop_remote_tunnel=stop_remote_tunnel, force=False)
        if self._serving_connections_tasks:
            # super().stop() close the connections, and therefore those should be closed right after
            await asyncio.gather(*self._serving_connections_tasks)

    async def _serve_new_connection(self, websocket: WebsocketType):
        """
        Serve a new connection with the given websocket until eof reached.
        Then, mark the websocket_done_event
        """
        # We don't want to establish connections with the exit address and port if the websocket we received
        # is still queued on the remote InputTunnel. Therefore, we wait for an initial message before connecting.
        message: aiohttp.WSMessage = await websocket.receive()
        if websocket.closed:
            # Websocket was closed, we don't even start a connection
            return
        if message.type != aiohttp.WSMsgType.TEXT and message.data == WEBSOCKET_CONNECTION_START_MESSAGE:
            self._logger.error('Invalid first message: %s', message.data)
        try:
            reader, writer = await asyncio.open_connection(host=self._exit_address,
                                                           port=self._exit_port)
            connection = _ConnectionHandler(websocket=websocket, connection_reader=reader, connection_writer=writer,
                                            logger=self._logger)
            with object_in_list(connection, self._connections):
                await connection.run_until_eof()
        except ConnectionRefusedError:
            # No one is listening on the exit address and port, we close the tunnel normally.
            self._logger.debug('Connection Refused for `%s:%s`. Closing websocket', self._exit_address, self._exit_port)
        finally:
            await websocket.close()

    async def _run(self):
        """
        Wait for new websocket to arrive and open a connection object to (self._exit_address, self._exit_port).
        Then wait until either the connection or the websocket reach eof before marking the websocket as unneeded
        """
        while self.running:
            websocket = await self._get_websocket()
            task = asyncio.ensure_future(self._serve_new_connection(websocket))
            task_in_list_until_done(task, self._serving_connections_tasks)
