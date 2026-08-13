import pytest
import requests_mock

from livesim.api import AdminApi, ApiError

BASE = "http://api"


def api() -> AdminApi:
    return AdminApi(BASE, "admin", "pw")


def envelope(records, page: int = 0, total_pages: int = 1) -> dict:
    return {
        "records": records,
        "totalSize": len(records),
        "totalPages": total_pages,
        "page": page,
        "pageSize": 200,
    }


def device(device_id: str, site_id: str = "site-1", status: str = "ONLINE") -> dict:
    return {
        "deviceId": device_id,
        "deviceType": "FIXED",
        "siteId": site_id,
        "status": status,
        "model": "AQ-1",
        "fwVersion": "1.0.0",
    }


def site(site_id: str = "site-1", facility_type: str = "OFFICE") -> dict:
    return {"siteId": site_id, "siteName": "본사", "facilityType": facility_type}


def login_response(mock, token: str = "admin-jwt"):
    return mock.post(
        f"{BASE}/auth/login", json={"accessToken": token, "role": "ADMIN"}
    )


# ---- 로그인 ------------------------------------------------------------


def test_login_stores_access_token():
    with requests_mock.Mocker() as mock:
        login_response(mock)
        client = api()
        client.login()

        assert client.token == "admin-jwt"
        assert mock.last_request.json() == {"username": "admin", "password": "pw"}


def test_login_failure_does_not_leak_submitted_password():
    body = {
        "errorCode": "VALIDATION_FAILED",
        "fieldErrors": [
            {"field": "password", "message": "too short", "rejectedValue": "hunter2"}
        ],
    }
    with requests_mock.Mocker() as mock:
        mock.post(f"{BASE}/auth/login", status_code=422, json=body)

        with pytest.raises(ApiError) as excinfo:
            api().login()

        assert "hunter2" not in str(excinfo.value)
        assert "VALIDATION_FAILED" in str(excinfo.value)


def test_unreachable_backend_becomes_a_readable_api_error():
    """백엔드가 아직 안 뜬 것은 흔한 운영 상황 — 트레이스백이 아니라 원인 한 줄."""
    import requests

    with requests_mock.Mocker() as mock:
        mock.post(f"{BASE}/auth/login", exc=requests.exceptions.ConnectionError)

        with pytest.raises(ApiError, match="백엔드 통신 실패"):
            api().login()


def test_connection_error_during_provisioning_is_catchable_by_backoff():
    """디바이스별 프로비저닝 실패는 런너의 백오프가 흡수해야 한다."""
    import requests

    with requests_mock.Mocker() as mock:
        login_response(mock)
        mock.post(
            f"{BASE}/admin/devices/AQ-1/secret", exc=requests.exceptions.ConnectTimeout
        )

        client = api()
        client.login()

        with pytest.raises(ApiError):
            client.provision_device_token("AQ-1")


def test_non_admin_role_is_warned_not_rejected(caplog):
    """역할 이름은 백엔드 버전마다 늘어난다 — 하드 매칭으로 막지 않는다."""
    with requests_mock.Mocker() as mock:
        mock.post(f"{BASE}/auth/login", json={"accessToken": "t", "role": "SERVICE"})
        api().login()

    assert any("SERVICE" in record.getMessage() for record in caplog.records)


# ---- 목록 조회 ---------------------------------------------------------


def test_list_devices_reads_envelope_records():
    with requests_mock.Mocker() as mock:
        login_response(mock)
        mock.get(f"{BASE}/admin/devices", json=envelope([device("AQ-1")]))

        client = api()
        client.login()

        assert [d["deviceId"] for d in client.list_devices()] == ["AQ-1"]


def test_list_devices_walks_every_page():
    pages = [
        envelope([device("AQ-1")], page=0, total_pages=2),
        envelope([device("AQ-2")], page=1, total_pages=2),
    ]

    def respond(request, context):
        return pages[int(request.qs["page"][0])]

    with requests_mock.Mocker() as mock:
        login_response(mock)
        mock.get(f"{BASE}/admin/devices", json=respond)

        client = api()
        client.login()

        assert [d["deviceId"] for d in client.list_devices()] == ["AQ-1", "AQ-2"]


def test_pagination_stops_on_empty_page_despite_total_pages():
    """totalPages가 실제 데이터보다 크면 같은 빈 페이지를 무한 재요청하게 된다."""
    def respond(request, context):
        page = int(request.qs["page"][0])
        records = [device("AQ-1")] if page == 0 else []
        return envelope(records, page=page, total_pages=99)

    with requests_mock.Mocker() as mock:
        login_response(mock)
        mock.get(f"{BASE}/admin/devices", json=respond)

        client = api()
        client.login()

        assert len(client.list_devices()) == 1


def test_non_envelope_response_is_rejected():
    with requests_mock.Mocker() as mock:
        login_response(mock)
        mock.get(f"{BASE}/admin/devices", json=[device("AQ-1")])

        client = api()
        client.login()

        with pytest.raises(ApiError, match="envelope"):
            client.list_devices()


def test_expired_token_triggers_relogin_and_retry():
    with requests_mock.Mocker() as mock:
        mock.post(
            f"{BASE}/auth/login",
            [
                {"json": {"accessToken": "old", "role": "ADMIN"}},
                {"json": {"accessToken": "fresh", "role": "ADMIN"}},
            ],
        )
        mock.get(
            f"{BASE}/admin/devices",
            [
                {"status_code": 401, "json": {}},
                {"json": envelope([device("AQ-1")])},
            ],
        )

        client = api()
        client.login()
        devices = client.list_devices()

        assert [d["deviceId"] for d in devices] == ["AQ-1"]
        assert client.token == "fresh"
        logins = [r for r in mock.request_history if r.path == "/auth/login"]
        assert len(logins) == 2
        assert mock.request_history[-1].headers["Authorization"] == "Bearer fresh"


def test_persistent_401_is_reported_not_retried_forever():
    with requests_mock.Mocker() as mock:
        login_response(mock)
        mock.get(f"{BASE}/admin/devices", status_code=401, json={})

        client = api()
        client.login()

        with pytest.raises(ApiError, match="401"):
            client.list_devices()


# ---- 디바이스 선별 -----------------------------------------------------


def resolve(mock, devices, sites, **kwargs):
    login_response(mock)
    mock.get(f"{BASE}/admin/devices", json=envelope(devices))
    mock.get(f"{BASE}/admin/sites", json=envelope(sites))
    client = api()
    client.login()
    return client.resolve_devices(**kwargs)


def test_resolve_joins_facility_type_from_site():
    with requests_mock.Mocker() as mock:
        resolved = resolve(mock, [device("AQ-1")], [site(facility_type="SCHOOL")])

    assert len(resolved) == 1
    assert resolved[0].facility_type == "SCHOOL"
    assert resolved[0].device_type == "FIXED"


def test_resolve_keeps_offline_devices():
    """장시간 중단 후 재시작하면 health checker가 전부 OFFLINE으로 내려놓는다."""
    with requests_mock.Mocker() as mock:
        resolved = resolve(mock, [device("AQ-1", status="OFFLINE")], [site()])

    assert [d.device_id for d in resolved] == ["AQ-1"]


def test_resolve_skips_maintenance_devices():
    with requests_mock.Mocker() as mock:
        resolved = resolve(
            mock,
            [device("AQ-1"), device("AQ-2", status="MAINTENANCE")],
            [site()],
        )

    assert [d.device_id for d in resolved] == ["AQ-1"]


def test_resolve_applies_exclude_list():
    with requests_mock.Mocker() as mock:
        resolved = resolve(
            mock, [device("AQ-1"), device("AQ-2")], [site()], exclude=("AQ-2",)
        )

    assert [d.device_id for d in resolved] == ["AQ-1"]


def test_resolve_applies_max_devices_deterministically():
    with requests_mock.Mocker() as mock:
        resolved = resolve(
            mock,
            [device("AQ-3"), device("AQ-1"), device("AQ-2")],
            [site()],
            max_devices=2,
        )

    assert [d.device_id for d in resolved] == ["AQ-1", "AQ-2"]


def test_resolve_skips_device_whose_site_is_unknown():
    """facility 세그먼트를 추측하면 EMQX 룰이 매칭되지 않아 조용히 유실된다."""
    with requests_mock.Mocker() as mock:
        resolved = resolve(mock, [device("AQ-1", site_id="ghost")], [site("site-1")])

    assert resolved == []


# ---- 프로비저닝 --------------------------------------------------------


def test_issue_device_secret_sends_bearer_token():
    with requests_mock.Mocker() as mock:
        login_response(mock)
        mock.post(
            f"{BASE}/admin/devices/AQ-1/secret", status_code=201, json={"secret": "s3"}
        )

        client = api()
        client.login()

        assert client.issue_device_secret("AQ-1") == "s3"
        assert mock.last_request.headers["Authorization"] == "Bearer admin-jwt"


def test_exchange_device_token_uses_snake_case_and_no_auth_header():
    with requests_mock.Mocker() as mock:
        login_response(mock)
        mock.post(f"{BASE}/auth/device/token", json={"access_token": "device-jwt"})

        client = api()
        client.login()

        assert client.exchange_device_token("AQ-1", "s3") == "device-jwt"
        assert mock.last_request.json() == {"device_id": "AQ-1", "secret": "s3"}
        assert "Authorization" not in mock.last_request.headers


def test_provision_calls_secret_then_token_in_order():
    with requests_mock.Mocker() as mock:
        login_response(mock)
        mock.post(f"{BASE}/admin/devices/AQ-1/secret", json={"secret": "s3"})
        mock.post(f"{BASE}/auth/device/token", json={"access_token": "device-jwt"})

        client = api()
        client.login()

        assert client.provision_device_token("AQ-1") == "device-jwt"
        paths = [r.path for r in mock.request_history]
        assert paths == ["/auth/login", "/admin/devices/aq-1/secret", "/auth/device/token"]


def test_provision_failure_raises():
    with requests_mock.Mocker() as mock:
        login_response(mock)
        mock.post(f"{BASE}/admin/devices/AQ-1/secret", status_code=403, json={})

        client = api()
        client.login()

        with pytest.raises(ApiError, match="시크릿 발급 실패"):
            client.provision_device_token("AQ-1")
