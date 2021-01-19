from typing import Any, Union, Iterable
from aiohttp.web import WebSocketResponse
from aiohttp import ClientWebSocketResponse, ClientSession, BasicAuth, ClientError, TCPConnector

from .exceptions import NETunnelInvalidProxy

import logging
import socket
import asyncio
import contextlib
import functools
import sys
import http

LOGGING_FORMATTER = '%(asctime)s %(name)s - %(levelname)s - %(message)s'


def asyncio_current_task(loop=None):
    if sys.version_info >= (3, 9, 0):
        return asyncio.current_task(loop)
    return asyncio.Task.current_task(loop)


def asyncio_all_tasks(loop=None):
    if sys.version_info >= (3, 9, 0):
        return asyncio.all_tasks(loop)
    return asyncio.Task.all_tasks(loop)


async def get_session(headers=None, ssl=None) -> ClientSession:
    """
    Return an aiohttp.ClientSession object.
    ClientSession must be initialized inside an async context.
    :param headers: Optional headers to append for every request that this session will make
    :param ssl: SSLContext object. False to skip validation, None for default SSL check.
    """
    connector = TCPConnector(ssl=ssl)
    headers = headers or {}
    return ClientSession(connector=connector, ws_response_class=EventClientWebSocketResponse, headers=headers)


def get_logger(name, level=logging.INFO):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(LOGGING_FORMATTER)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


async def run_blocking_func_in_executor(func, *args, **kwargs):
    """
    Run a blocking function in a different executor so that it won't stop
    the event loop
    """
    return await asyncio.get_event_loop().run_in_executor(None, functools.partial(func, *args, **kwargs))


def get_unused_port(min_port: int, max_port: int, exclude_ports: Iterable = None, address='127.0.0.1'):
    """
    Return an unused port from a range of integers
    :param min_port: minimum port that can be allocated
    :param max_port: maximum port that can be allocated
    :param exclude_ports: list of ports to exclude
    :param address: address of the interface to try binding with
    """
    exclude_ports = set(exclude_ports) or set()
    for port in range(min_port, max_port):
        if port in exclude_ports:
            continue
        with contextlib.suppress(OSError):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((address, port))
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                return port
    raise RuntimeError(f"Failed to found an available port between {min_port} and {max_port}")


@contextlib.contextmanager
def object_in_list(obj: Any, list_of_obj: list):
    """
    append obj to list_of_obj and remove it once the with statement finishes.
    Used like this:

    with object_in_list(item, list_of_items):
        # item is in list_of_items
    # item is not in list_of_items
    """
    list_of_obj.append(obj)
    try:
        yield obj
    finally:
        with contextlib.suppress(ValueError):
            list_of_obj.remove(obj)


def task_in_list_until_done(task: Union[asyncio.Task, asyncio.Future], list_of_tasks: list):
    """
    Add a task to a list and a callback which removes it from the list once it's done
    """
    def remove_task(*args):
        with contextlib.suppress(ValueError):
            list_of_tasks.remove(task)
    list_of_tasks.append(task)
    task.add_done_callback(remove_task)


def update_dict_recursively(dict_to_update: dict, dict_new_values: dict):
    """
    Update the target dictionary recursively
    """
    for key, value in dict_new_values.items():
        if isinstance(value, dict):
            # If the target corresponding key is not a dict as well, we'll override it
            # because it's irrelevant and then we can use update_dict_recursively again
            if key not in dict_to_update or not isinstance(dict_to_update[key], dict):
                dict_to_update[key] = {}
            update_dict_recursively(dict_to_update[key], value)
        else:
            dict_to_update[key] = value


class EventItem(asyncio.Event):
    def __init__(self, loop=None):
        """
        Works just like asyncio.Event with the following enhancements:
        - Stores an item when setting the event and retrieve it when it's available with self.wait
        """
        super().__init__(loop=loop)
        self._obj: Any = None

    def set(self, obj: Any = None):
        self._obj = obj
        return super().set()

    def clear(self):
        self._obj = None
        return super().clear()

    async def wait(self) -> Any:
        await super().wait()
        return self._obj


class EventQueue(asyncio.Queue):
    """
    This queue works just like a normal async queue, a new method self.join_no_empty
    was added. It works just like self.join but in opposite, it returns whenever
    a new item was put in the queue without extracting it
    """
    def __init__(self, maxsize=0):
        super().__init__(maxsize=maxsize)
        self._queue_no_empty = asyncio.Event()

    async def put(self, item):
        await super().put(item)
        self._queue_no_empty.set()

    async def get(self):
        try:
            return await super().get()
        finally:
            if self.qsize() == 0:
                self._queue_no_empty.clear()

    def get_nowait(self):
        try:
            return super().get_nowait()
        finally:
            if self.qsize() == 0:
                self._queue_no_empty.clear()

    async def join_no_empty(self):
        """
        Blocks until a new task is in the queue
        """
        await self._queue_no_empty.wait()


class EventWebSocketResponse(WebSocketResponse):
    """
    Works the same as WebSocketResponse with new methods:
    - wait_closed - block until websocket is close (does not trigger close)
    - close_nowait - trigger close websocket without blocking
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._shutdown = asyncio.Event()
        self._closing_task: asyncio.Future = None

    async def wait_closed(self):
        """
        Block until the websocket is closed
        """
        await self._shutdown.wait()
        if self._closing_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._closing_task

    def close_nowait(self):
        """
        Closing without waiting.
        Use wait_closed to await for the close task
        """
        self._closing_task = asyncio.ensure_future(self.close())

    async def close(self, *args, **kwargs):
        return_value = await super().close(*args, **kwargs)
        self._shutdown.set()
        return return_value


class EventClientWebSocketResponse(ClientWebSocketResponse):
    """
    Works the same as ClientWebSocketResponse with new methods:
    - wait_closed - block until websocket is close (does not trigger close)
    - close_nowait - trigger close websocket without blocking
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._shutdown = asyncio.Event()
        self._closing_task: asyncio.Future = None

    async def wait_closed(self):
        """
        Block until the websocket is closed
        """
        await self._shutdown.wait()
        if self._closing_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._closing_task

    def close_nowait(self):
        """
        Closing without waiting.
        Use wait_closed to await for the close task
        """
        self._closing_task = asyncio.ensure_future(self.close())

    async def close(self, *args, **kwargs):
        return_value = await super().close(*args, **kwargs)
        self._shutdown.set()
        return return_value


async def verify_proxy(proxy_url, test_url, username=None, password=None):
    """
    Verify that a proxy server is working
    :param proxy_url: url to the proxy
    :param test_url: url that accepts GET method and will be used to test the proxy
    :param username: username to use to authenticate the proxy
    :param password: password to use to authenticate the proxy
    """
    proxy_auth = None
    if username and password:
        proxy_auth = BasicAuth(username, password)
    elif (username and password is None) or (password and username is None):
        raise ValueError('HTTP proxy authentication should include both username and password')
    async with ClientSession() as client:
        try:
            async with client.get(test_url, proxy=proxy_url, proxy_auth=proxy_auth) as resp:
                if resp.status != http.HTTPStatus.OK.value:
                    raise NETunnelInvalidProxy(f"Invalid response from proxy: {resp.status}")
        except (ClientError, ConnectionRefusedError) as err:
            raise NETunnelInvalidProxy(str(err))
