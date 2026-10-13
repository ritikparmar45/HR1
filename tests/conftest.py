"""Pytest configuration and test client fixtures."""
import os
import pytest
from fastapi.testclient import TestClient
from pymongo import MongoClient

# Point test suite to an isolated test database
os.environ["MONGO_DB"] = "attendance_test_db"

from app.main import app, client, db, init_indexes


@pytest.fixture(scope="session", autouse=True)
def setup_test_database():
    """Ensure indexes exist and clean test collections before testing."""
    init_indexes()
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


@pytest.fixture
def client_app():
    """FastAPI TestClient instance."""
    with TestClient(app) as c:
        yield c
