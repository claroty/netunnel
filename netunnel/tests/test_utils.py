from urllib.parse import urlparse
from netunnel.common import utils
from netunnel.common.exceptions import NETunnelInvalidProxy
from .utils import ProxyForTests

import asyncio
import pytest


def test_object_in_list(bytes_data):
    l = []
    with utils.object_in_list(bytes_data, l):
        assert bytes_data in l
    assert bytes_data not in l


@pytest.mark.parametrize("test_target_dict,test_target_source,expected", [
    ({}, {}, {}),
    ({'a': 'a'}, {'a': 'b'}, {'a': 'b'}),
    ({}, {'a': 'a'}, {'a': 'a'}),
    ({'a': {'a': 'a'}}, {'a': {'b': 'b'}}, {'a': {'a': 'a', 'b': 'b'}}),
    ({'a': 'a'}, {'a': {'a': 'a'}}, {'a': {'a': 'a'}})
])
def test_update_dict_recursively(test_target_dict, test_target_source, expected):
    utils.update_dict_recursively(test_target_dict, test_target_source)
    assert test_target_dict == expected


async def test_task_in_list_until_done():
    l = []
    task = asyncio.ensure_future(asyncio.sleep(0.5))
    utils.task_in_list_until_done(task, l)
    assert task in l
    await task
    assert task not in l


async def test_event_item(bytes_data):
    event_item = utils.EventItem()
    assert not event_item.is_set()
    event_item.set()
    assert await event_item.wait() is None
    assert event_item.is_set()
    event_item.clear()
    assert not event_item.is_set()
    event_item.set(bytes_data)
    assert event_item.is_set()
    assert await event_item.wait() == bytes_data
    event_item.clear()
    event_item.set()
    assert await event_item.wait() is None


async def test_event_queue(bytes_data):
    event_queue = utils.EventQueue()
    join_new_task_task = asyncio.ensure_future(event_queue.join_no_empty())
    await event_queue.put(bytes_data)
    try:
        await asyncio.wait_for(join_new_task_task, timeout=1)
    except asyncio.TimeoutError:
        pytest.fail("EventQueue.join_no_empty blocks after a new task inserted")
    returned_random_bits = await event_queue.get()
    assert returned_random_bits == bytes_data
    event_queue.task_done()
    join_new_task_task = asyncio.ensure_future(event_queue.join_no_empty())
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(join_new_task_task, timeout=0.5)


async def test_verify_proxy(aiohttp_unused_port):
    port = aiohttp_unused_port()
    test_data = {
        'proxy_url': f'http://localhost:{port}',
        'test_url': 'http://www.google.com/'
    }
    test_url_hostname = urlparse(test_data['test_url']).hostname
    with ProxyForTests(port=port) as proxy:
        await utils.verify_proxy(**test_data)
        proxy.assert_host_forwarded(test_url_hostname)
    with pytest.raises(ValueError):
        await utils.verify_proxy(**test_data, username='a')
    with pytest.raises(ValueError):
        await utils.verify_proxy(**test_data, password='a')
    with pytest.raises(NETunnelInvalidProxy):
        await utils.verify_proxy(**test_data)
    port = aiohttp_unused_port()
    test_data['proxy_url'] = f'http://localhost:{port}'
    with ProxyForTests(port=port, username='abc', password='abc') as proxy:
        await utils.verify_proxy(**test_data, username='abc', password='abc')
        proxy.assert_host_forwarded(test_url_hostname)
