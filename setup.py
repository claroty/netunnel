#!/usr/bin/env python3

from setuptools import setup, find_packages

import pathlib

HERE = pathlib.Path(__file__).parent


def read(path):
    return (HERE / path).read_text("utf-8").strip()


install_requires = [
    'aiohttp>=3.5.4,<4.0.0',
    'aiofiles>=0.0.4',
    'pymongo<=3.11.2',
    'marshmallow<=3.10.0',  # We have temporary backwards compatibility for 2.X, but also support 3.X
    'cryptography>=2.8',
    'colorama<=0.4.4',
    'click<=7.2',
    'importlib-metadata<=3.4.0'
]

setup(
    name="netunnel",
    version='1.0.4',
    description='A tool to create network tunnels over HTTP/S written in Python 3',
    long_description="\n\n".join((read("README.md"), read("CHANGES.md"))),
    long_description_content_type='text/markdown',
    author='Claroty Open Source',
    author_email='opensource@claroty.com',
    maintainer='Claroty Open Source',
    maintainer_email='opensource@claroty.com',
    url='https://github.com/claroty/netunnel',
    license="Apache 2",
    packages=find_packages(exclude=('*test*',)),
    install_requires=install_requires,
    include_package_data=True,
    classifiers=[
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: OS Independent",
    ],
    python_requires='>=3.6',
)
