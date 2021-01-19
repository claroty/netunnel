#!/usr/bin/env python3

from setuptools import setup, find_packages

install_requires = [
    'aiohttp<4.0.0',
    'aiofiles',
    'pymongo',
    'marshmallow',  # We have temporary backwards compatibility for 2.X, but also support 3.X
    'cryptography',
    'colorama',
    'click'
]

setup(
    name="netunnel",
    version='1.0.2',
    description='SSL tunnels utilities',
    author='Claroty Ltd.',
    author_email='pypi@claroty.com',
    packages=find_packages(exclude=('*test*',)),
    install_requires=install_requires,
    include_package_data=True,
    classifiers=[
        "Programming Language :: Python :: 3",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.6',
)
