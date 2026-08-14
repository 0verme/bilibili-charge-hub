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
    assert response.json()["milestone"] == "M10-single-instance"


def test_security_headers_and_api_cache_control() -> None:
    with TestClient(create_app()) as client:
        response = client.get("/healthz")
        api_response = client.get("/api/system/capabilities")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert api_response.headers["cache-control"] == "no-store"


def test_dashboard_script_localizes_scheduled_job_names() -> None:
    with TestClient(create_app()) as client:
        script = client.get("/static/dashboard.js")

    assert script.status_code == 200
    assert "charge_collection:'充电记录采集'" in script.text
    assert "coupon_claim:'B 币券领取'" in script.text
    assert "notification_retry:'通知失败重试'" in script.text


def test_share_script_formats_charge_time_in_application_timezone() -> None:
    with TestClient(create_app()) as client:
        script = client.get("/static/share.js")

    assert script.status_code == 200
    assert "new Intl.DateTimeFormat('zh-CN'" in script.text
    assert "formatTime(item.charged_at,data.timezone)" in script.text


def test_share_page_contains_dashboard_sections() -> None:
    template = __import__("pathlib").Path("app/templates/share.html").read_text(encoding="utf-8")

    assert "Bilibili 充电驾驶舱" in template
    assert 'id="trend-chart"' in template
    assert 'id="supporter-ranking"' in template
    assert 'id="monthly-bars"' in template
    assert 'id="recent-records"' in template
