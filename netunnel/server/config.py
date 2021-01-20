import os
import copy
import json
import time
import asyncio
import aiofiles
import contextlib
import click

from ..common.utils import run_blocking_func_in_executor

# The default config is the basic configuration of the netunnel.
# In case the configuration already exists, it's performing an update over the default config.
# This way, in case the default configuration has a new key in some version, it will be added automatically.
# We don't performing a full recursive merge between the current and the default configuration to protect
# dict values like 'allowed_tunnel_destination' from being changed unexpectedly.
_DEFAULT_CONFIG = {
    'allowed_tunnel_destinations': {"127.0.0.1": "*"},
    'http_proxy': None,
    'http_proxy_test_url': 'https://google.com',
    'peers': [],
    'allow_unverified_ssl_peers': False,
    'revision': 1
    # secret_key is not provided here so it won't be dynamically stored in the configuration file by mistake
}
ENV_VARIABLES_PREFIX = 'NETUNNEL_'


def get_default_config(use_env_vars=True) -> dict:
    """
    If use_env_vars is True, config values will be override by environment variables if defined.
    For example, to override the value of "allowed_tunnel_destinations", export "NETUNNEL_ALLOWED_TUNNEL_DESTINATIONS".
    Environment variables are expected to be in json format, and are usually defined in the systemd service.
    """
    default_config = copy.deepcopy(_DEFAULT_CONFIG)
    if use_env_vars:
        for key in default_config:
            env_var_name = ENV_VARIABLES_PREFIX + key.upper()
            if env_var_name in os.environ:
                default_config[key] = json.loads(os.environ[env_var_name])
    return default_config


class NETunnelConfiguration:
    def __init__(self, config_path=None):
        """
        USE ONLY `await NETunnelConfiguration.create(path)` TO INITIALIZE AN OBJECT

        Manage configurations for NETunnelServer in JSON format.
        Store configurations in memory unless config_path was given.
        :param config_path: Optional path to a configuration file in which to store changes.
        """
        self._config_path = config_path
        # This lock is used to prevent multiple disk writes when saving the configuration using safe writes.
        self._saving_config_lock = asyncio.Lock()
        self._config = get_default_config()

    @classmethod
    async def create(cls, config_path=None):
        self = NETunnelConfiguration(config_path=config_path)
        await self._initialize()
        return self

    async def _initialize(self):
        """
        Initialize config by loading config_path
        """
        if self._config_path:
            with contextlib.suppress(FileNotFoundError):
                async with aiofiles.open(self._config_path) as config_file:
                    data = await config_file.read()
                    self._config.update(json.loads(data))
        await self.save()

    async def save(self):
        """
        Save configurations using safe writes to the config path if exists
        """
        if self._config_path is None:
            return
        async with self._saving_config_lock:
            temp_config_path = f"{self._config_path}.{time.time()}"
            # First we write all the config to a temporary file
            async with aiofiles.open(temp_config_path, 'w') as temp_config_file:
                await temp_config_file.write(json.dumps(self._config, indent=4))
                await temp_config_file.flush()
                await run_blocking_func_in_executor(os.fsync, temp_config_file)
            # Now we overwrite the config file with an atomic operation
            await run_blocking_func_in_executor(os.rename, temp_config_path, self._config_path)

    def __getitem__(self, key):
        return self._config[key]

    def __setitem__(self, key, value):
        self._config[key] = value

    def __contains__(self, key):
        return key in self._config

    def get(self, key, default=None):
        return self._config.get(key, default)


@click.group()
def main():
    pass


@main.command()
@click.argument('path')
@click.option('--custom-changes', default='{}', help='A json dump string of the changes you want to make in the default configuration')
def create(path, custom_changes):
    custom_changes = json.loads(custom_changes)

    config_dir = os.path.dirname(path)
    os.makedirs(config_dir, exist_ok=True)
    with open(path, 'w') as f:
        json.dump(custom_changes, f)

    async def create_config():
        config = await NETunnelConfiguration.create(path)
        await config.save()

    loop = asyncio.get_event_loop()
    loop.run_until_complete(create_config())


if __name__ == '__main__':
    main()
