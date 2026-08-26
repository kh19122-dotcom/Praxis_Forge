from __future__ import annotations

import httpx
import pytest

from scenario_runner.http import ServiceClient
from tests.fake_services import FakeForge


@pytest.fixture
def forge() -> FakeForge:
    return FakeForge()


@pytest.fixture
def booking_client(forge: FakeForge) -> ServiceClient:
    client = httpx.Client(
        transport=httpx.MockTransport(forge.booking.handle),
        base_url="http://booking.test",
    )
    return ServiceClient("booking", "http://booking.test", client=client)


@pytest.fixture
def pvs_client(forge: FakeForge) -> ServiceClient:
    client = httpx.Client(
        transport=httpx.MockTransport(forge.pvs.handle),
        base_url="http://pvs.test",
    )
    return ServiceClient("pvs", "http://pvs.test", client=client)
