from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz_does_not_expose_configuration() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_home_and_capabilities_are_available() -> None:
    with TestClient(create_app()) as client:
        assert client.get("/").status_code == 200
        response = client.get("/api/system/capabilities")

    assert response.status_code == 200
    assert response.json()["milestone"] == "M3"
