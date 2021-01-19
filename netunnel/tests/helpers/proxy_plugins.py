import os

from typing import Optional
from proxy.http.parser import HttpParser
from proxy.http.proxy import HttpProxyBasePlugin


ACCESS_LOG_KEY = 'ACCESSED_HOSTS_LOG'


class LogAccessedHostsPlugin(HttpProxyBasePlugin):
    """
    log client connection access to the log path in ACCESS_LOG_KEY environment variable.
    This plugin helps to overcome the normal behavior which log accesses only after the client closed the connection,
    which causes race conditions for when we inspect the log file later.
    """
    def before_upstream_connection(
            self, request: HttpParser) -> Optional[HttpParser]:
        return request

    def handle_client_request(
            self, request: HttpParser) -> Optional[HttpParser]:
        try:
            with open(os.environ[ACCESS_LOG_KEY] ,'a') as f:
                f.write(request.host.decode() + '\n')
        except KeyError:
            raise RuntimeError('ACCESSED_HOSTS_LOG was not defined')
        return request

    def handle_upstream_chunk(self, chunk: memoryview) -> memoryview:
        return chunk

    def on_upstream_connection_close(self) -> None:
        pass