# NETunnel
NETunnel is a tool to create network tunnels over HTTP/S written in Python 3.\
It can be used both in a client-server and in a server-server model. 

## Getting Started

### Installing
```bash
pip install netunnel
```
We officially support Python 3.6+.

### Usage
The following example creates an HTTP tunnel from the client to the server's port 22 (SSHD service).

Running the server: (In production, use --config-path to preserve changes)
```bash
$ python -m netunnel.server
The server is running in stateless mode. Use --config-path to generate a config file
netunnel_server - INFO - Generating default secret-key
netunnel_server - INFO - Starting server on 127.0.0.1:4040
```

Running the client:
```bash
$ python -m netunnel.client --remote-port 22
Opening tunnel to the server...
Tunnel entrance socket: 127.0.0.1:54781
Tunnel exit socket: 127.0.0.1:22
```

The server's SSHD service is now accessible from the client:
```bash
$ ssh -p 54781 localhost
```

Please take a look at the [examples](examples) directory for additional usages.

## How it works
1. The client connects to the server and creates a websocket connection we call a "Channel". The channel
is used by server to send commands to the client and it performs heartbeat pings so both sides will know
if there are connection issues.
2. The client makes a POST request to create a tunnel.
3. Either the client or the server (depends on the tunnel's direction) listens on a socket locally
for incoming connections.
4. For every new connection, it generates a websocket connection to the remote, and stream the data
from the connection over to the websocket and vice versa.
5. Whenever a websocket is created, the remote connects to a socket locally and stream data from the
websocket to the socket and vice versa.

```
Connection1 ->                      Websocket1 ->               ->
Connection2 ->  Tunnel Entrance ->  Websocket2 ->  Tunnel Exit  -> Service
Connection3 ->                      Websocket3 ->               ->
```

### Under the hood
There are 2 core objects which performs the tunnelling:

- **InputTunnel** - The tunnel's entrance which listens on a socket.
- **OutputTunnel** - The tunnel's exit which creates connections to a socket.

When a normal tunnel is created, the client creates an InputTunnel and the server creates
an OutputTunnel, while reverse tunnels are essentially the server is creating InputTunnel while
the client is creating an OutputTunnel.

InputTunnel is initialized with a feeder of websockets that the client/server provides, so
that the implementation can be generic. In reverse tunnels, The server uses the channel to request
the client for a new websocket when it needs to feed a new websocket.

## Additional Features
* Peers - A NETunnel server can register remote NETunnel servers called peers. The peers are stored in the
configuration and can be used to create static tunnels.
* Static Tunnels - NETunnel supports permanent tunnels between servers. This is a useful feature
for when we want a long term tunnels between machines. It can be used by making the netunnel server to run as a service
and create a configuration file with peers and static tunnels which initialized on startup. Both peers and static tunnels
can also be created dynamically via the server's API.
An example for a server-server model can be found here: [examples/server-server](examples/server-server)
* HTTP proxy support - You can use `--proxy-url` `--proxy-username` `--proxy-password` to configure a proxy
for the client. When used in a server-server model, there can be a global proxy used by the server to connect with.
The credentials of the global proxy in that case are stored encrypted in the server's configuration using a secret_key.
* Authentication plugins - By default, no authentication is made between NETunnel instances.
This can be configured by inherit the auth classes on [netunnel/common/auth.py](netunnel/common/auth.py) and pass them
to the client and server using `--auth-plugin`. A plugin example: [examples/secret-auth-plugin](examples/secret-auth-plugin)
