from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fake_pvs.app import app, store
from fake_pvs.settings import Settings


@pytest.fixture
def client() -> TestClient:
    store.settings = Settings(seed="obj-002", state_path=None)
    store.reset()
    with TestClient(app) as test_client:
        yield test_client
    store.reset()
