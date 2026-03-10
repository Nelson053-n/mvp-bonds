"""
Test fixtures for the MVP LLM Portfolio application.
"""

from pathlib import Path
import tempfile
from typing import AsyncGenerator, Generator

import pytest
import pytest_asyncio

from app.config import Settings
from app.main import app
from app.services.storage_service import StorageService
from app.services.cache_service import CacheService
from app.services.portfolio_service import PortfolioService
from app.services.moex_service import MOEXService
from app.services.llm_service import LLMService

TEST_JWT_SECRET = "test_jwt_secret_for_tests_32chars_long"


@pytest.fixture(scope="session")
def test_db_path() -> Generator[str, None, None]:
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_portfolio.db"
        yield str(db_path)


@pytest.fixture
def settings_override(test_db_path: str) -> Generator[Settings, None, None]:
    """Override settings for testing."""
    from app import config

    original_settings = config.settings
    test_settings = Settings(
        moex_base_url="https://iss.moex.com/iss",
        sqlite_db_path=test_db_path,
        llm_mode="stub",
        openai_api_key=None,
        openai_base_url="https://api.openai.com/v1",
        openai_model="gpt-4o-mini",
        log_level="DEBUG",
        log_format="text",
        jwt_secret=TEST_JWT_SECRET,
    )
    config.settings = test_settings
    yield test_settings
    config.settings = original_settings


@pytest.fixture
def storage_service(settings_override: Settings) -> Generator[StorageService, None, None]:
    """Create a fresh storage service for testing."""
    service = StorageService()
    yield service
    # Cleanup: delete all items
    with service._connect() as conn:
        conn.execute("DELETE FROM portfolio_items")
        conn.commit()


@pytest.fixture
def cache_service() -> Generator[CacheService, None, None]:
    """Create a fresh cache service for testing."""
    service = CacheService()
    yield service
    service.stop_background()


@pytest.fixture
def moex_service() -> MOEXService:
    """Create MOEX service for testing."""
    return MOEXService()


@pytest.fixture
def llm_service() -> LLMService:
    """Create LLM service for testing."""
    return LLMService()


@pytest.fixture
def portfolio_service(
    storage_service: StorageService,
    cache_service: CacheService,
    moex_service: MOEXService,
    llm_service: LLMService,
) -> PortfolioService:
    """Create portfolio service with mocked dependencies."""
    return PortfolioService()


@pytest.fixture
def test_auth_token(settings_override: Settings) -> str:
    """Generate a valid JWT token for the bootstrap admin user (id=1)."""
    from app.services.auth_service import AuthService
    svc = AuthService()
    return svc.create_token(user_id=1, username="admin", is_admin=True)


@pytest.fixture
def auth_headers(test_auth_token: str) -> dict:
    """HTTP headers with valid Bearer token."""
    return {"Authorization": f"Bearer {test_auth_token}"}


@pytest_asyncio.fixture
async def client(settings_override: Settings) -> AsyncGenerator:
    """Create async test client with settings override applied."""
    from httpx import AsyncClient, ASGITransport

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def sample_stock_input() -> dict:
    """Sample stock input for testing."""
    return {
        "ticker": "SBER",
        "quantity": 100,
        "purchase_price": 250.0,
    }


@pytest.fixture
def sample_bond_input() -> dict:
    """Sample bond input for testing."""
    return {
        "ticker": "SU26238RMFS4",
        "quantity": 10,
        "purchase_price": 920.0,
    }
