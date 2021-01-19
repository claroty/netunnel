import jwt
import time

from aiohttp import web
from netunnel.common.auth import NETunnelClientAuth, NETunnelServerAuth
from netunnel.common.utils import get_session


class MockClientAuth(NETunnelClientAuth):
	def __init__(self, secret, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._secret = secret
		self._token = None

	async def authenticate(self, client, *args, **kwargs):
		payload = {'secret': self._secret}
		session = await get_session(ssl=False)
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
		return {'secret': self._secret}


class MockServerAuth(NETunnelServerAuth):
	def __init__(self, secret, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self._secret = secret

	async def get_client_for_peer(self, secret):
		return MockClientAuth(secret=secret)

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
		response = {'token': jwt.encode({'exp': time.time() + 30}, self._secret).decode()}
		return web.json_response(response)
