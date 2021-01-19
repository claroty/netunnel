import http


class NETunnelError(Exception):
    pass


class NETunnelNotConnectedError(NETunnelError):
    pass


class NETunnelNotAuthenticatedError(NETunnelError):
    pass


class NETunnelResponseError(NETunnelError):
    pass


class NETunnelDestinationNotAllowed(NETunnelError):
    pass


class NETunnelInvalidProxy(NETunnelError):
    pass


class NETunnelServerError(NETunnelError):
    """
    Used by the server to raise exception and convert them to json responses with a specific http status code bound to them
    """
    status_code = http.HTTPStatus.INTERNAL_SERVER_ERROR


class NETunnelServerNotFound(NETunnelServerError):
    status_code = http.HTTPStatus.NOT_FOUND


class NETunnelServerBadRequest(NETunnelServerError):
    status_code = http.HTTPStatus.BAD_REQUEST


class NETunnelAuthError(NETunnelServerError):
    status_code = http.HTTPStatus.FORBIDDEN
