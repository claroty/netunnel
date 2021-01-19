from enum import Enum
from typing import Any, Callable, Awaitable
from collections import defaultdict
from .const import WebsocketType
from .utils import EventItem

import bson  # This is provided by pymongo
import aiohttp
import logging
import asyncio


class Messages(Enum):
    TUNNEL_SERVER_TO_CLIENT_NEW_CONNECTION = 0  # Request from server to client to open a new websocket since there is a new connection to the tunnel.
    DELETE_TUNNEL = 1  # Request from the server to client to delete a tunnel on the client side

##### Serialize and Deserialize Messages bson support #####

MESSAGES_TYPES_KEY = "__Messages__"
def _encode_message_data(data: dict):
    """
    Recursively encoding the data to support Messages objects
    """
    result = {}
    for key, value in data.items():
        if isinstance(value, Messages):
            result[key] = {MESSAGES_TYPES_KEY: value.name}
        elif isinstance(value, dict):
            result[key] = _encode_message_data(value)
        else:
            result[key] = value
    return result

def _decode_message_data(data: dict):
    """
    Recursively decoding the data to support Messages objects
    """
    result = {}
    for key, value in data.items():
        if isinstance(value, dict):
            if MESSAGES_TYPES_KEY in value:
                result[key] = getattr(Messages, value[MESSAGES_TYPES_KEY])
            else:
                result[key] = _decode_message_data(value)
        else:
            result[key] = value
    return result

def message_bson_dumps(data: dict) -> bytes:
    """
    Return a bytes dump of data using bson.encode with support to Message objects
    """
    encoded_data = _encode_message_data(data)
    return bson.encode(encoded_data)

def message_bson_loader(data: bytes) -> dict:
    """
    Return a dictionary of data using bson.loads with support to Message objects
    """
    data = bson.decode(data)
    return _decode_message_data(data)

################################################################


class ChannelError(Exception):
    pass


class ChannelMessage:
    def __init__(self, message_type: Messages, data: dict = None, _identifier: int = None):
        """
        Represent a request over the control channel. Each request has a type, and optionally additional data.
        The identifier is used to match ChannelMessage with a ChannelResponse as request <-> response relationship
        :param message_type: Messages object
        :param data: data to send. Must be serializable to json.
        :param _identifier: This is used internally
        """
        self.message_type = message_type
        self.data = data or {}
        self._identifier = _identifier
        self.channel: Channel = None

    def get_error_response(self, err_message, data: Any = None) -> 'ChannelResponse':
        """
        Generates an ChannelResponse for this message with error message
        """
        if self._identifier is None:
            raise ChannelError('Missing _identifier to generate a response. This is probably a bug')
        return ChannelResponse(message_identifier=self._identifier, data=data, error=err_message)

    def get_valid_response(self, data: Any = None) -> 'ChannelResponse':
        """
        Generates and ChannelResponse for this message the the given data
        """
        if self._identifier is None:
            raise ChannelError('Missing _identifier to generate a response. This is probably a bug')
        return ChannelResponse(message_identifier=self._identifier, data=data)

    def to_dict(self):
        return {
            '_type': 'message',
            '_identifier': self._identifier,
            'message_type': self.message_type,
            'data': self.data
        }

    @staticmethod
    def from_dict(message) -> 'ChannelMessage':
        return ChannelMessage(**message)


class ChannelResponse:
    def __init__(self, message_identifier: int, data: dict = None, error: Any = None):
        """
        Represent a response over the control channel. A response is always associated with an ChannelMessage identifier.
        Do not initialize it directly, use ChannelMessage.get_error_response and ChannelMessage.get_valid_response instead
        """
        self._message_identifier = message_identifier
        self.data = data or {}
        self.error = error

    def is_ok(self):
        return self.error is None

    def to_dict(self):
        response = {
            '_type': 'response',
            '_identifier': self._message_identifier,
            'data': self.data
        }
        if self.error is not None:
            response['error'] = self.error
        return response

    @staticmethod
    def from_dict(response) -> 'ChannelResponse':
        message_identifier = response.pop('_identifier')
        return ChannelResponse(message_identifier=message_identifier, **response)


class Channel:
    def __init__(self, websocket: WebsocketType, channel_id: int, handler: 'Callable[[ChannelMessage], Awaitable[ChannelResponse]]' = None, logger: logging.Logger = None):
        """
        Creates a link between 2 peers on top of a websocket.
        Websockets communication is bi-directional, each message is independent so we can't tell which message is a request and
        which is a response. This creates a protocol on top of a websocket to solve this problem.
        :param websocket: An established websocket between 2 servers. Channel is responsible for closing it
        :param handler: an awaitable callable to handle ChannelMessages. If left as None, we won't handle incoming messages, but we'll still be able to send
        :param channel_id: a unique id for that Channel.
        :param logger: additional logger
        """
        self._websocket = websocket
        self._handler = handler
        self._id = channel_id
        self._logger = logger or logging.getLogger(__name__)
        self._message_id_counter = 0
        # A dictionary of {message_id: Event}. This dict register message_ids which awaits for response.
        # When a new ChannelResponse arrives, if it has a message_id in this dict, we use the event to notify the subscriber.
        self._subscribed_messages = defaultdict(EventItem)
        # This lock is used when registering new message on self._subscribed_messages to prevent duplications of
        # message_ids in case multiple messages received at once.
        self._subscribe_messages_lock = asyncio.Lock()

    @property
    def id(self):
        return self._id

    @property
    def running(self):
        return not self._websocket.closed

    async def send_message(self, message: 'ChannelMessage', timeout=None, raise_if_error=False) -> 'ChannelResponse':
        """
        Send an ChannelMessage to the remote peer and wait for a response.
        """
        if self._websocket.closed:
            raise ChannelError('Channel is closed')
        # Subscribe the message so we'll be notified when a response come back
        async with self._subscribe_messages_lock:
            self._message_id_counter += 1
            message_id = self._message_id_counter
            self._subscribed_messages[message_id].clear()

        # Mark the message_id before sending it
        message_payload = message.to_dict()
        message_payload['_identifier'] = message_id
        try:
            await self._websocket.send_bytes(message_bson_dumps(message_payload))
            # wait for the response to arrive
            wait_coro = self._subscribed_messages[message_id].wait()
            if timeout:
                wait_coro = asyncio.wait_for(wait_coro, timeout=timeout)
            response: ChannelResponse = await wait_coro
        finally:
            del self._subscribed_messages[message_id]
        if raise_if_error:
            if not response.is_ok():
                raise ChannelError(response.error)
        return response

    async def serve(self):
        """
        Start listening for incoming traffic from the websocket.
        """
        async for msg in self._websocket:
            msg: aiohttp.WSMessage
            if msg.type != aiohttp.WSMsgType.BINARY:
                # Websocket related traffic. We ignore it
                self._logger.debug('Channel `%s` Received message of type `%s` with data `%s`. Ignoring', self._id, msg.type, msg.data)
                continue
            message = message_bson_loader(msg.data)
            message_type = message.pop('_type')
            if message_type == 'message':
                # This is a message. We'll handle it and return a response. If no handler, a valid response will return
                message = ChannelMessage.from_dict(message)
                if self._handler:
                    # Inject Channel to the ChannelMessage so that the handler can access it
                    message.channel = self
                    try:
                        response = await self._handler(message)
                    except Exception as err:
                        self._logger.exception('Got exception while handling channel message of type `%s` for channel `%s`:', message.message_type.name, self.id)
                        response = message.get_error_response(err_message=str(err))
                else:
                    response = message.get_valid_response()
                await self._websocket.send_bytes(message_bson_dumps(response.to_dict()))
            elif message_type == 'response':
                message_identifier = message['_identifier']
                if message_identifier in self._subscribed_messages:
                    # We'll pass the message to the subscriber
                    response = ChannelResponse.from_dict(message)
                    self._subscribed_messages[message_identifier].set(response)
                else:
                    self._logger.warning('No subscriber for the message: %s', message)
            else:
                self._logger.warning('Unknown message type: %s', message_type)
        if self._websocket.exception():
            raise self._websocket.exception()
        try:
            close_reason = aiohttp.WSCloseCode(self._websocket.close_code)
            if close_reason is not aiohttp.WSCloseCode.OK:
                self._logger.debug('Channel `%s` closed unexpectedly with close code: %s(%s)', self.id, close_reason.value, close_reason.name)
        except ValueError:
            self._logger.debug('Channel `%s` closed unexpectedly with unknown close code: %s', self.id, self._websocket.close_code)

    async def close(self):
        """
        Closes the websocket, and therefore the channel itself.
        """
        await self._websocket.close()