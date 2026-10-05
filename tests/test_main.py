import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient
from engine.core.models import InterpretResponse, DimensionChange


@pytest.fixture()
def client():
    from main import app
    return TestClient(app)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_interpret_returns_changes(client):
    mock_result = InterpretResponse(
        changes=[DimensionChange(name="WIDTH@Mirror", value_meters=0.762)],
        explanation="30 inches",
    )
    with patch("main.run_interpret", new=AsyncMock(return_value=mock_result)):
        response = client.post("/interpret", json={
            "instruction": "set width to 30 inches",
            "dimensions": [{"name": "WIDTH@Mirror", "value_meters": 0.5}],
        })
    assert response.status_code == 200
    data = response.json()
    assert data["changes"][0]["name"] == "WIDTH@Mirror"
    assert data["changes"][0]["value_meters"] == pytest.approx(0.762)
    assert data["error"] is None


def test_interpret_returns_error(client):
    mock_result = InterpretResponse(error="Width too small")
    with patch("main.run_interpret", new=AsyncMock(return_value=mock_result)):
        response = client.post("/interpret", json={
            "instruction": "tiny",
            "dimensions": [],
        })
    assert response.status_code == 200
    data = response.json()
    assert data["error"] == "Width too small"
    assert data["changes"] == []


def test_interpret_rejects_missing_instruction(client):
    response = client.post("/interpret", json={"dimensions": []})
    assert response.status_code == 422
