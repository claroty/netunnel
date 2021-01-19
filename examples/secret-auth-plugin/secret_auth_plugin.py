"""
This is an example for NETunnel auth plugin. it enforces clients to include a secret that only the server knows
in order to be authorized with it.
Please make sure to read netunnel/common/auth.py first so you would understand when each method is called in
NETunnel's core code.
"""
import jwt
import time

from aiohttp import web
from netunnel.common.auth import NETunnelClientAuth, NETunnelServerAuth
from netunnel.common.utils import get_session
from netunnel.client import NETunnelClient


class SecretClientAuth(NETunnelClientAuth):
    # This object will be used by NETunnelClient

	def __init__(self, secret, *args, **kwargs):
        # The `secret` parameter is exclusive to this plugin. you can declare whatever parameters you need
        # as long as your users knows what parameters your plugin expects.
		super().__init__(*args, **kwargs)
		self._secret = secret
		self._token = None

	async def authenticate(self, client: NETunnelClient, *args, **kwargs):
        # `client` will always be passed to authenticate(), but it cannot be used to make requests to the server, this
        # should be handled manually. The reason for that is that we're not authenticated yet of course.
		payload = {'secret': self._secret}
		session = await get_session()
		async with session:
			async with session.post(f'{client.server_url}/authenticate', json=payload, raise_for_status=True) as resp:
				data = await resp.json()
				self._token = data['token']

	async def is_authenticated(self):
		if self._token is None:
			return False
		try:
			result = jwt.decode(self._token.encode(), self._secret)
		except (ValueError, jwt.DecodeError):
			return False
		return result['exp'] > time.time()

	async def get_authorized_headers(self):
		return {'Authorization': f'Bearer {self._token}'}

	def dump_object(self):
        # The return value here is stored as `auth_data` of peers that needs the secret for the next startup.
        # In a real life use case, you would probably store something else then the secret since it will be written
        # to the disk (for example an encrypted version of it), just make sure __init__ can be initialized with
        # whatever you return here.
		return {'secret': self._secret}


class SecretServerAuth(NETunnelServerAuth):
    # This object will be used by NETunnelServer

	def __init__(self, secret, *args, **kwargs):
        # The `secret` parameter is exclusive to this plugin. you can declare whatever parameters you need
        # as long as your users knows what parameters your plugin expects.
		super().__init__(*args, **kwargs)
		self._secret = secret

	async def get_client_for_peer(self, secret):
		return SecretClientAuth(secret=secret)

	async def is_authenticated(self, request: web.Request):
		if 'Authorization' not in request.headers:
			return False
		try:
			auth_type, token = request.headers['Authorization'].split()
			result = jwt.decode(token.encode(), self._secret)
		except (ValueError, jwt.DecodeError):
			return False
		if result['exp'] < time.time():
			return False
		return True

	async def authenticate(self, request: web.Request):
		data = await request.json()
		if data['secret'] != self._secret:
			return web.HTTPForbidden()
		# We're generating a token valid for the next 30 minutes. The tunnel won't be closed if this expires,
		# but the client will not be able to make valid requests with this token again, which will be resolved
		# by it automatically since it will request a new one.
		response = {'token': jwt.encode({'exp': time.time() + 30}, self._secret).decode()}
		return web.json_response(response)

