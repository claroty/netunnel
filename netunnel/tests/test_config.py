import re

from netunnel.server import config

import os
import json
import time

from netunnel.server.config import get_default_config, ENV_VARIABLES_PREFIX
from netunnel.tests.utils import environment_variables


async def test_config_path_created(config_path):
    await config.NETunnelConfiguration.create(config_path=config_path)
    assert os.path.exists(config_path)


async def test_write_config(config_path):
    netunnel_config = await config.NETunnelConfiguration.create(config_path=config_path)
    with open(config_path) as f:
        assert json.load(f) == get_default_config()
    data = str(time.time())
    netunnel_config[data] = data
    await netunnel_config.save()
    with open(config_path) as f:
        assert json.load(f)[data] == data


async def test_default_config_with_variables(config_path):
    default_conf_without_env = get_default_config(use_env_vars=False)
    some_key = next(iter(default_conf_without_env.keys()))
    env_var_name = ENV_VARIABLES_PREFIX + some_key.upper()
    with environment_variables({env_var_name: '{"something": 1}'}):
        await config.NETunnelConfiguration.create(config_path=config_path)
        with open(config_path) as f:
            config_from_disk = json.load(f)
        assert config_from_disk == get_default_config(use_env_vars=True)
        assert config_from_disk[some_key] == json.loads(os.environ[env_var_name])
        assert default_conf_without_env[some_key] != config_from_disk[some_key]


def test_verify_all_config_keys_are_overridable(config_path):
    """
    This test makes sure that all the keys has valid name for env variable,
    so one can use an env variable to override them if he wants.
    """
    default_conf_without_env = get_default_config(use_env_vars=False)
    for key in default_conf_without_env:
        assert re.match('^[a-z][a-zA-Z0-9_]+$', key), f"Key {key} in config is not valid for environment variable name"


async def test_load_config(config_path):
    now_as_string = str(time.time())
    data = {now_as_string: now_as_string}
    with open(config_path, 'w') as f:
        f.write(json.dumps(data))
        f.flush()
        os.fsync(f.fileno())
    netunnel_config = await config.NETunnelConfiguration.create(config_path=config_path)
    assert netunnel_config[now_as_string] == now_as_string


async def test_create_config_doesnt_change_allowed_tunnel_destinations(config_path):
    """
    Make sure the allowed tunnel destination is not being merged with the default value when loading a config from disk.
    """
    default_config = get_default_config()
    # if someone changes this key name some day they should the test too.
    assert 'allowed_tunnel_destinations' in default_config
    test_allowed_destinations = {'8.8.8.8': '1,2,3'}
    # the allowed destinations of the test must be different than the default
    assert test_allowed_destinations != default_config['allowed_tunnel_destinations']

    with open(config_path, 'w') as f:
        f.write(json.dumps({'allowed_tunnel_destinations': test_allowed_destinations}))
    netunnel_config = await config.NETunnelConfiguration.create(config_path=config_path)
    # make sure the config contains the original value and didn't modify it
    assert netunnel_config['allowed_tunnel_destinations'] == test_allowed_destinations
    assert netunnel_config['allowed_tunnel_destinations'] != default_config['allowed_tunnel_destinations']
