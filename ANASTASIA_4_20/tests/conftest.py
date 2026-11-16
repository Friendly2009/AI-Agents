"""Fail immediately on any attempted network connection during tests."""
import socket


def pytest_sessionstart(session):
    def blocked(*args, **kwargs):
        raise AssertionError('Tests must use mocks; network access is disabled')
    socket.socket.connect = blocked
    socket.socket.connect_ex = blocked
    socket.create_connection = blocked
