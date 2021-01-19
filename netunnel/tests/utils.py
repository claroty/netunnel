import contextlib
import os
import proxy
import pytest
import socket
import asyncio
import tempfile

from .helpers.proxy_plugins import ACCESS_LOG_KEY


PACKET_CHUNK_SIZE = 4096


class ProxyForTests(proxy.Proxy):
	def __init__(self, port, username=None, password=None):
		self._port = port
		credentials = []
		if None not in (username, password):
			credentials = ['--basic-auth', f'{username}:{password}']
		self._temp_dir = tempfile.TemporaryDirectory()
		os.environ[ACCESS_LOG_KEY] = self.log_path
		super().__init__(['--hostname', '127.0.0.1', '--port', str(port),
						  '--plugins', 'netunnel.tests.helpers.proxy_plugins.LogAccessedHostsPlugin',
						  '--num-workers', '1', *credentials])
	@property
	def log_path(self):
		if self._temp_dir:
			return os.path.join(self._temp_dir.name, 'test_proxy_access_hosts.log')

	def assert_host_forwarded(self, hostname):
		"""
		Assert that data is a substring in the proxy's log
		"""
		with open(self.log_path) as f:
			logs = f.read()
			assert hostname in logs, f'`{hostname}` not found in proxy logs:\n{logs}'

	def __exit__(self, exc_type, exc_val, exc_tb):
		self._temp_dir.cleanup()
		super().__exit__(exc_type, exc_val, exc_tb)


async def echo_handler(reader, writer):
	data = await reader.read(1024)
	writer.write(data)
	await writer.drain()
	writer.close()


async def assert_tunnel_echo_server(tunnel_entrance_address, tunnel_entrance_port,
									tunnel_exit_address, tunnel_exit_port, bytes_data):
	"""
	Setup echo server handler on the the tunnel exit socket and try to communicate with it
	through the entrance socket
	"""
	server = await asyncio.start_server(echo_handler, host=tunnel_exit_address, port=tunnel_exit_port)
	reader, writer = await asyncio.open_connection(host=tunnel_entrance_address, port=tunnel_entrance_port)
	writer.write(bytes_data)
	try:
		assert await asyncio.wait_for(reader.read(1024), timeout=5) == bytes_data, "Invalid response from tunnel"
	except asyncio.TimeoutError:
		pytest.fail(f"Tunnel response failed to come back after 5 seconds")
	writer.close()
	server.close()
	await server.wait_closed()


def assert_tunnel_not_listening(tunnel_local_address, tunnel_local_port):
	with pytest.raises(ConnectionRefusedError):
		with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
			assert sock.connect(
				(tunnel_local_address, tunnel_local_port)) == 0, f"Tunnel not listening on {tunnel_local_address}:{tunnel_local_port}"


@contextlib.contextmanager
def environment_variables(env_vars: dict):
	original_env = dict(os.environ)
	os.environ.update(env_vars)
	try:
		yield
	finally:
		os.environ.clear()
		os.environ.update(original_env)
