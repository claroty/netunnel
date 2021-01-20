from typing import TYPE_CHECKING
from aiohttp import web
if TYPE_CHECKING:
    from ..client import NETunnelClient

import abc


class NETunnelServerAuth(abc.ABC):
    def __init__(self, *args, **kwargs):
        pass

    @abc.abstractmethod
    async def get_client_for_peer(self, *args, **kwargs) -> 'NETunnelClientAuth':
        """
        Returns an instance of NETunnelClientAuth which will be used by a peer object to authenticate with it.
        The parameters of the subclass should be the expected parameters of the constructor of your NETunnelClientAuth class:
        for example:

        async def get_client_for_peer(self, username, password):
            return NETunnelClientAuth(username, password)
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def is_authenticated(self, request: web.Request) -> bool:
        """
        Return True if the request is authorized and False if not.
        The headers of the request should include the return value of NETunnelClientAuth.get_authorized_headers()
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def authenticate(self, request: web.Request):
        """
        Perform the server-side authentication. This method handling the route /authenticate on the NETunnel server.

        The NETunnelClientAuth.authenticate(client) should make a request to the /authenticate uri of the NETunnel server,
        and this method will be called as the route handler.
        """
        raise NotImplementedError


class NETunnelClientAuth(abc.ABC):
    def __init__(self, *args, **kwargs):
        pass

    @abc.abstractmethod
    async def authenticate(self, client: 'NETunnelClient', *args, **kwargs):
        """
        Perform the client-side authentication. The NETunnel server is exposing the /authenticate uri which will
        be handled by NETunnelServerAuth.authenticate(request).
        Raises NETunnelAuthError if failed to authenticate.

        After calling this method, self.get_authorized_headers is expected to return valid headers which will
        be authorized by the remote.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def is_authenticated(self) -> bool:
        """
        Return True if we are authenticated with the remote.
        This should return False if the tokens returned by the server are expired.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def get_authorized_headers(self) -> dict:
        """
        Return headers which can be used to make authorized requests to the remote server.
        Raises NETunnelNotAuthenticatedError if we need to authenticate first/again
        """
        raise NotImplementedError

    @abc.abstractmethod
    def dump_object(self) -> dict:
        """
        This method is not async since we're using marshmallow to parse this data and it does not support asyncio yet.
        Return a dump of this NETunnelClientAuth instance's data so it can be stored in the configuration
        and reloaded during startup. This is used by NETunnelServer when handling peer objects.
        The return value should be a dictionary that can be given to NETunnelServerAuth.get_client_for_peer() and
        will generate a valid NETunnelClientAuth object.
        """
        raise NotImplementedError


class ServerNoAuth(NETunnelServerAuth):
    async def get_client_for_peer(self, *args, **kwargs) -> 'NETunnelClientAuth':
        return ClientNoAuth(*args, **kwargs)

    async def is_authenticated(self, request: web.Request):
        return True

    async def authenticate(self, request: web.Request):
        pass


class ClientNoAuth(NETunnelClientAuth):
    async def authenticate(self, client: 'NETunnelClient', *args, **kwargs):
        pass

    async def is_authenticated(self):
        return True

    async def get_authorized_headers(self):
        return {}

    def dump_object(self):
        return {}
