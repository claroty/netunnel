# NETunnel Client-Server example
In this example, you have a `server.py` and a `client.py` files.
They contain a very minimalist code for running a NETunnelServer and a NETunnelClient using their python objects.

The server.py also creates a `netunnel.conf` with the default configurations if there isn't one already.

## Usage
Run the server:
```bash
python server.py
```

Run the client:
```bash
python client.py
```
In this example, the client always opens a tunnel on port 12345 to the server on port 22:
```bash
ssh -p 12345 localhost
```