# NETunnel Secret Auth Plugin
This is an example of an authentication plugin for NETunnel.
The plugin initialize the NETunnel server with a secret of your choosing and only clients which knows the
secret can open tunnels with the server.

This plugin requires pyjwt since its using JWT tokens for the authentication, so make sure you install it first:
```bash
pip install "pyjwt<2.0.0"
```

# Usage
Run the server. Our secret will be `abc` for this example:
```bash
python -m netunnel.server --auth-plugin secret_auth_plugin.SecretServerAuth --auth-data '{"secret": "abc"}'
```
Run the client:
```bash
python -m netunnel.client --auth-plugin secret_auth_plugin.SecretClientAuth --auth-data '{"secret": "abc"}'
```
If you try to run the client without the plugin or with different secret, you'll receive Forbidden (403) respond.