# NETunnel Server-Server example
In this example we'll create 2 instances of NETunnel servers, `server1` and `server2`,
each with its own configuration file.

`server2` settings will include a single peer - `server1` with a single static tunnel to this peer.

There are several advantages in using server-server model instead of the [client-server](../client-server) model.
First of all, A single server instance can create more than 1 tunnel to more then 1 remote, and secondly, these
tunnels are designed to recreate themselves if there was a disconnection.

Another useful advantage is that peers and static tunnels can be dynamically created
using the NETunnelClient.
## Usage
Run the first server:
```bash
python -m netunnel.server -p 4040 -c server1.conf
```
Run the second server:
```bash
python -m netunnel.server -p 4041 -c server2.conf
```
The `server2.conf` configuration file include a peer called `server1` with a static tunnel to it on port 20000. Let's
make sure it works:
```bash
ssh -p 20000 localhost
```
Notice that if you stop the instance of `server1` from running, the second instance will try to reconnect it.

Now, make sure we have both `server1` and `server2` running and let's use `server1`'s API to add `server2` as a
peer and a static tunnel to it:
```bash
python client.py
```
Now `server2` is registered as a new peer and you will see that `server1.conf` was updated. The local port
that `server1` chooses for the tunnel is an available port number from 20000 or more, so we'll assume it chose port 20001
and now we can run:
```bash
ssh -p 20001 localhost
```
You can also see that `server1.conf` was updated so it will recreate the tunnel on startup.