"""Fail immediately on any attempted network connection during tests."""
import socket
import sys


def pytest_sessionstart(session):
    original_connect = socket.socket.connect
    def blocked(*args, **kwargs):
        # Windows asyncio builds its private wakeup socket pair over loopback.
        if sys.platform == 'win32' and len(args) > 1 and isinstance(args[1], tuple) and args[1][0] == '127.0.0.1':
            return original_connect(*args, **kwargs)
        raise AssertionError('Tests must use mocks; network access is disabled')
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.create_connection = blocked
