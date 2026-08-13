import pytest
import requests
import requests_mock

from livesim.api import ApiError, exchange_device_token

BASE = "http://api"
TOKEN_URL = f"{BASE}/auth/device/token"


def test_exchange_returns_access_token():
    with requests_mock.Mocker() as mock:
        mock.post(TOKEN_URL, json={"access_token": "device-jwt"})

        assert exchange_device_token(BASE, "AQ-01", "s3cr3t") == "device-jwt"


def test_exchange_uses_snake_case_body_and_no_auth_header():
    """device_id/secret 자체가 자격증명 — 인증 헤더를 실으면 오히려 401이 난다."""
    with requests_mock.Mocker() as mock:
        mock.post(TOKEN_URL, json={"access_token": "device-jwt"})
        exchange_device_token(BASE, "AQ-01", "s3cr3t")

        assert mock.last_request.json() == {"device_id": "AQ-01", "secret": "s3cr3t"}
        assert "Authorization" not in mock.last_request.headers


def test_trailing_slash_in_base_url_is_tolerated():
    with requests_mock.Mocker() as mock:
        mock.post(TOKEN_URL, json={"access_token": "t"})
        exchange_device_token(f"{BASE}/", "AQ-01", "s3cr3t")

        assert mock.last_request.path == "/auth/device/token"


def test_rejected_secret_carries_4xx_status():
    with requests_mock.Mocker() as mock:
        mock.post(TOKEN_URL, status_code=401, json={"errorCode": "UNAUTHORIZED"})

        with pytest.raises(ApiError) as excinfo:
            exchange_device_token(BASE, "AQ-01", "wrong")

        assert excinfo.value.status == 401
        assert excinfo.value.is_rejected is True


def test_server_error_is_not_treated_as_rejection():
    """5xx는 백엔드 일시 장애일 수 있으므로 재시도 대상이어야 한다."""
    with requests_mock.Mocker() as mock:
        mock.post(TOKEN_URL, status_code=503, json={})

        with pytest.raises(ApiError) as excinfo:
            exchange_device_token(BASE, "AQ-01", "s3cr3t")

        assert excinfo.value.is_rejected is False


def test_unreachable_backend_is_readable_and_retryable():
    with requests_mock.Mocker() as mock:
        mock.post(TOKEN_URL, exc=requests.exceptions.ConnectionError)

        with pytest.raises(ApiError) as excinfo:
            exchange_device_token(BASE, "AQ-01", "s3cr3t")

        assert "백엔드 통신 실패" in str(excinfo.value)
        assert excinfo.value.status is None
        assert excinfo.value.is_rejected is False


def test_error_does_not_leak_submitted_secret():
    """422의 rejectedValue에는 제출한 secret이 그대로 담겨 온다."""
    body = {
        "errorCode": "VALIDATION_FAILED",
        "fieldErrors": [
            {"field": "secret", "message": "invalid", "rejectedValue": "s3cr3t"}
        ],
    }
    with requests_mock.Mocker() as mock:
        mock.post(TOKEN_URL, status_code=422, json=body)

        with pytest.raises(ApiError) as excinfo:
            exchange_device_token(BASE, "AQ-01", "s3cr3t")

        assert "s3cr3t" not in str(excinfo.value)
        assert "VALIDATION_FAILED" in str(excinfo.value)


def test_no_admin_surface_remains():
    """0.2.0의 존재 이유 — admin API 코드가 남아 있으면 안 된다."""
    import livesim.api as api

    for name in ("AdminApi", "login", "resolve_devices", "issue_device_secret"):
        assert not hasattr(api, name), name
