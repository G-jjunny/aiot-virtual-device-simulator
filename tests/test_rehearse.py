"""보안 리허설 — 모든 케이스는 '거부되어야 PASS'다."""

from livesim.api import ApiError
from livesim.config import DeviceCredential, Settings
from livesim.rehearse import ACCEPTED, REJECTED, UNREACHABLE, rehearse

SETTINGS = Settings(
    api_base_url="http://api",
    mqtt_host="broker",
    mqtt_port=1883,
    devices_file="devices.yaml",
    control_dir="control",
)

INVENTORY = (
    DeviceCredential(
        device_id="AQ-01",
        secret="real-secret",
        site_id="S-1",
        device_type="FIXED",
        facility_type="OFFICE",
    ),
)


def rejecting_exchange(status: int = 401):
    def exchange(base_url, device_id, secret):
        raise ApiError(f"거부 ({status})", status=status)

    return exchange


def accepting_exchange(base_url, device_id, secret):
    return "token-that-should-not-exist"


def rejecting_probe(host, port, username, password):
    return REJECTED


def accepting_probe(host, port, username, password):
    return ACCEPTED


def unreachable_probe(host, port, username, password):
    return UNREACHABLE


def run(exchange=None, probe=None, inventory=INVENTORY):
    return rehearse(
        SETTINGS,
        inventory,
        exchange=exchange or rejecting_exchange(),
        probe=probe or rejecting_probe,
    )


def by_id(results):
    return {item.case_id: item for item in results}


def test_all_cases_pass_when_everything_is_rejected():
    results = run()

    assert [item.case_id for item in results] == ["SEC-01", "SEC-02", "SEC-03"]
    assert all(item.passed for item in results)


def test_unknown_device_receiving_a_token_is_a_failure():
    results = by_id(run(exchange=accepting_exchange))

    assert results["SEC-01"].passed is False
    assert "등록되지 않은" in results["SEC-01"].detail


def test_wrong_secret_receiving_a_token_is_a_failure():
    results = by_id(run(exchange=accepting_exchange))

    assert results["SEC-02"].passed is False
    assert "AQ-01" in results["SEC-02"].detail


def test_forged_jwt_accepted_by_broker_is_a_failure():
    results = by_id(run(probe=accepting_probe))

    assert results["SEC-03"].passed is False
    assert "수락" in results["SEC-03"].detail


def test_unreachable_broker_is_not_a_pass():
    """브로커가 꺼져 있는 것은 '막혔다'는 증거가 아니다.

    이걸 PASS로 세면 인프라가 없을 때 리허설이 통과해버려, 아무것도 검증하지
    않은 결과가 '정상'으로 보고된다.
    """
    results = by_id(run(probe=unreachable_probe))

    assert results["SEC-03"].passed is False
    assert "닿지 못해" in results["SEC-03"].detail


def test_server_error_cannot_prove_rejection():
    """5xx는 '막혔다'는 증거가 아니다 — 통과로 세면 결함을 놓친다."""
    results = by_id(run(exchange=rejecting_exchange(500)))

    assert results["SEC-01"].passed is False
    assert "확인할 수 없음" in results["SEC-01"].detail


def test_real_secret_is_never_submitted():
    submitted: list[tuple[str, str]] = []

    def recording_exchange(base_url, device_id, secret):
        submitted.append((device_id, secret))
        raise ApiError("거부 (401)", status=401)

    run(exchange=recording_exchange)

    assert all(secret != "real-secret" for _, secret in submitted)


def test_empty_inventory_fails_the_secret_case_explicitly():
    results = by_id(run(inventory=()))

    assert results["SEC-01"].passed is True  # 인벤토리와 무관한 케이스
    assert results["SEC-02"].passed is False
    assert "인벤토리가 비어" in results["SEC-02"].detail


def test_forged_jwt_case_uses_a_registered_device_id():
    """존재하지 않는 이름으로 시도하면 '이름이 틀려서' 막힌 것과 구분되지 않는다."""
    seen: list[str] = []

    def recording_probe(host, port, username, password):
        seen.append(username)
        return False

    run(probe=recording_probe)

    assert seen == ["AQ-01"]
