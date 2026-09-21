"""Import the HA-independent modules without running the package __init__ (it imports HA)."""

import pathlib
import sys
import types

import pytest
from aiohttp import web

ROOT = pathlib.Path(__file__).resolve().parents[2]
pkg = types.ModuleType("ec")
pkg.__path__ = [str(ROOT / "custom_components" / "ews_calendar")]
sys.modules.setdefault("ec", pkg)
sys.path.insert(0, str(ROOT / "tests"))

import fake_ews


@pytest.fixture(autouse=True)
def _allow_sockets(request):
    """These tests run a fake server on localhost. When the Home Assistant pytest plugin is
    installed (the full test environment), sockets are blocked unless explicitly enabled."""
    try:
        request.getfixturevalue("socket_enabled")
    except pytest.FixtureLookupError:
        pass


@pytest.fixture
async def ews():
    """(server state, base URL)."""
    state = fake_ews.FakeEws()
    runner = web.AppRunner(state.app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield state, f"http://127.0.0.1:{port}"
    await runner.cleanup()
