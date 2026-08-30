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


def test_public_pages_link_to_github_project() -> None:
    from pathlib import Path

    expected_link = 'href="https://github.com/0verme/bilibili-charge-hub"'
    for template_name in ("home.html", "share.html"):
        template = Path("app/templates", template_name).read_text(encoding="utf-8")
        assert expected_link in template
        assert 'target="_blank"' in template
        assert 'rel="noopener noreferrer"' in template


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
    assert 'charge_collection: "充电记录采集"' in script.text
    assert 'coupon_claim: "B 币券领取"' in script.text
    assert 'notification_retry: "通知失败重试"' in script.text


def test_share_script_formats_charge_time_in_application_timezone() -> None:
    with TestClient(create_app()) as client:
        widgets = client.get("/static/dashboard-widgets.js")
        script = client.get("/static/share.js")

    assert widgets.status_code == 200
    assert script.status_code == 200
    assert "new Intl.DateTimeFormat('zh-CN'" in widgets.text
    assert "widgets.formatTime(item.charged_at,data.timezone)" in script.text


def test_share_page_contains_dashboard_sections() -> None:
    template = __import__("pathlib").Path("app/templates/share.html").read_text(encoding="utf-8")

    assert "Bilibili 充电驾驶舱" in template
    assert 'id="trend-chart"' in template
    assert 'id="supporter-ranking"' in template
    assert 'id="monthly-bars"' in template
    assert 'id="recent-records"' in template


def test_internal_dashboard_uses_shared_widgets_and_pagination() -> None:
    template = __import__("pathlib").Path("app/templates/dashboard.html").read_text(
        encoding="utf-8"
    )
    script = __import__("pathlib").Path("app/static/dashboard.js").read_text(encoding="utf-8")

    assert "/static/dashboard-widgets.js" in template
    assert 'id="supporter-ranking"' in template
    assert 'id="monthly-bars"' in template
    assert 'id="page-size"' in template
    assert 'item.remark || "—"' in script
    assert "widgets.renderSummary" in script
