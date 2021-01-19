from typing import Union, Callable, Coroutine, Any, Dict, List
from .utils import EventClientWebSocketResponse, EventWebSocketResponse
from aiohttp import ClientWebSocketResponse
from aiohttp.web import WebSocketResponse


# Types
WebsocketType = Union[EventClientWebSocketResponse, EventWebSocketResponse, WebSocketResponse, ClientWebSocketResponse]
CallableOfCoroutine = Callable[..., Coroutine]
TunnelId = int
ChannelId = int


# Constants
CLIENT_CHANNEL_HEARTBEAT = 20
SERVER_MAXIMUM_CLIENT_CHANNELS = 1000
SERVER_MAXIMUM_CLIENT_CHANNELS_ERROR = 'Reached maximum allowed number of control_channels'
MIN_STATIC_TUNNEL_LOCAL_PORT = 20000
MAX_STATIC_TUNNEL_LOCAL_PORT = 21000