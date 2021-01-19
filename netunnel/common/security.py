"""
Security utilities for netunnel
"""
from cryptography.fernet import Fernet

import click


@click.group(help="Toolkit for security utilities on netunnel")
def main():
    pass


@main.command(name='generate-key')
def _generate_key():
    """Generates a key that can be used on NETunnelServer
    """
    print(Encryptor.generate_key().decode())


@main.command(name='encrypt')
@click.argument('secret-key')
@click.argument('data')
def _encrypt(secret_key, data):
    """Encrypt data using the secret_key"""
    print(Encryptor(secret_key).encrypt_string(data))


@main.command(name='decrypt')
@click.argument('secret-key')
@click.argument('data')
def _decrypt(secret_key, data):
    """Decrypt data using the secret_key"""
    print(Encryptor(secret_key).decrypt_string(data))


class Encryptor:
    def __init__(self, key):
        self._fernet = Fernet(key)

    @staticmethod
    def generate_key():
        return Fernet.generate_key()

    def encrypt(self, data: bytes):
        return self._fernet.encrypt(data)

    def decrypt(self, data: bytes):
        return self._fernet.decrypt(data)

    def encrypt_string(self, data: str):
        return self.encrypt(data.encode()).decode()

    def decrypt_string(self, data: str):
        return self.decrypt(data.encode()).decode()


if __name__ == '__main__':
    main()