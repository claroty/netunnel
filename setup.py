#!/usr/bin/env python3

from setuptools import setup, find_packages
import os


# Extract version from package without importing it
here = os.path.abspath(os.path.dirname(__file__))
with open(os.path.join(here, 'netunnel', '__version__.py'), 'r', encoding='utf-8') as f:
	exec(f.read())

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
	version=locals()['__version__'],
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
