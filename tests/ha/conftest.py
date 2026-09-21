"""Fixtures for running the integration inside a real Home Assistant."""

from __future__ import annotations

import pathlib
import sys

import pytest
from aiohttp import web

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import fake_ews


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
async def ews(socket_enabled):
    """(server state, base URL) of a fake Exchange on localhost."""
    state = fake_ews.FakeEws()
    runner = web.AppRunner(state.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield state, f"http://127.0.0.1:{port}"
    await runner.cleanup()
