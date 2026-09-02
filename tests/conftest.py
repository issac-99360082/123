"""Test fixtures.

Every test runs against a fresh in-memory SQLite database and a TestClient
whose lifespan does not start the scheduler - tests drive `tick()` explicitly
so timing is deterministic.
"""

from __future__ import annotations

import os

os.environ.update(
    {
        "DATABASE_URL": "sqlite:///:memory:",
        "SCHEDULER_ENABLED": "false",
        "RESPECT_ROBOTS_TXT": "false",
        "MIN_SECONDS_BETWEEN_HITS_PER_HOST": "0",
        "LOG_LEVEL": "WARNING",
        "ENVIRONMENT": "development",
        "RATE_LIMIT_PER_MINUTE": "100000",
        "RATE_LIMIT_BURST": "100000",
    }
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402

get_settings.cache_clear()

from app.db import Base, SessionLocal, engine  # noqa: E402
from app.main import app as fastapi_app  # noqa: E402
from app.models import User  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_database():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def client():
    with TestClient(fastapi_app) as test_client:
        yield test_client


@pytest.fixture
def account(client) -> dict:
    """A signed-up user with an API key and ready-to-use auth headers."""
    response = client.post(
        "/v1/auth/signup", json={"email": "dev@example.com", "password": "correct-horse-42"}
    )
    assert response.status_code == 201, response.text
    key = response.json()["key"]
    return {"key": key, "headers": {"Authorization": f"Bearer {key}"}}


@pytest.fixture
def upgrade_to_business(db):
    """Grant a plan directly, the way a Stripe webhook would."""

    def _upgrade(email: str = "dev@example.com", plan: str = "business"):
        user = db.query(User).filter(User.email == email).one()
        user.plan = plan
        db.commit()
        return user

    return _upgrade
