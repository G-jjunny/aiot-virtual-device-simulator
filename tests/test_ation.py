"""에이티온 HTTP 수집 경로 — 값 모델과 송신."""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests
import requests_mock

from livesim import ation
from livesim.api import ApiError
from livesim.ation import (
    AtionDevice,
    battery_percent,
    blood_pressure,
    build_payload,
    epoch_millis,
    heart_rate_statistic,
    positioning,
    send,
    sleep_minutes,
    step_count,
)
from livesim.config import DeviceCredential
from livesim.profiles import evaluate

BASE = "http://api"
INGEST_URL = f"{BASE}/v1/biometric/ingest"
TS = datetime(2026, 7, 20, 14, 30, 0)

# 백엔드 골든 픽스처(aiot-be/aiot-api/src/test/resources/ation/golden-request.json)의
# 키 구조 스냅샷. 여기 값을 복제해 두는 이유는 이 저장소가 aiot-be 없이도 테스트되어야
# 하기 때문이고, 실제 파일이 옆에 있으면 아래 test_matches_backend_golden_fixture가
# 그 파일과 직접 대조한다.
GOLDEN_TOP = {"MESSAGE_ID", "device", "positioning", "statistics"}
GOLDEN_DEVICE = {
    "DEVICE_ID", "PHONE_NUMBER", "FW_VER", "PPG_HR", "PPG_SPO2", "IMU_STEP_CNT",
    "BP_SYS", "BP_DIA", "TEMPERATURE", "ENV TEMPERATURE", "ENV HUMIDITY",
    "SLEEP_DURATION", "WAKEUP_DURATION", "battery_percent", "MEASURED_AT",
}
GOLDEN_POS_INFO = {
    "latitude", "longitude", "accuracy_h", "latitude_coarse", "longitude_coarse",
    "altitude", "accuracy_v", "floor",
}
GOLDEN_STAT = {
    "name", "count", "average", "min", "max", "window_start", "window_end",
}

GOLDEN_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "aiot-be/aiot-api/src/test/resources/ation/golden-request.json"
)


def credential(device_id: str = "WB-01", **kwargs) -> DeviceCredential:
    fields = {
        "device_id": device_id,
        "secret": "",
        "site_id": "S-1",
        "device_type": "WEARABLE",
        "facility_type": "OFFICE",
        "transport": "ation_http",
    }
    fields.update(kwargs)
    return DeviceCredential(**fields)


def device_block(**kwargs) -> dict:
    return build_payload(credential(), TS, seed=3, **kwargs)["device"]


# ---- 페이로드 구조 -------------------------------------------------------


def test_payload_keys_match_golden_snapshot():
    payload = build_payload(credential(), TS, seed=3)

    assert set(payload) == GOLDEN_TOP
    assert set(payload["device"]) == GOLDEN_DEVICE
    pos_info = payload["positioning"]["location"]["result_data"]["pos_info"]
    assert set(pos_info) == GOLDEN_POS_INFO
    assert set(payload["statistics"][0]) == GOLDEN_STAT


@pytest.mark.skipif(
    not GOLDEN_FIXTURE.is_file(), reason="aiot-be 저장소가 옆에 없으면 건너뛴다"
)
def test_matches_backend_golden_fixture():
    """정본과 직접 대조. 백엔드 규격이 바뀌면 여기서 먼저 깨져야 한다."""
    golden = json.loads(GOLDEN_FIXTURE.read_text(encoding="utf-8"))
    payload = build_payload(credential(), TS, seed=3)

    assert set(payload) == set(golden)
    assert set(payload["device"]) == set(golden["device"])
    ours = payload["positioning"]["location"]["result_data"]["pos_info"]
    theirs = golden["positioning"]["location"]["result_data"]["pos_info"]
    assert set(ours) == set(theirs)
    assert set(payload["statistics"][0]) == set(golden["statistics"][0])


def test_spaced_keys_are_sent_verbatim():
    """공백이 든 키를 우리 관례로 고치면 백엔드가 그 값을 읽지 못한다."""
    device = device_block()

    assert "ENV TEMPERATURE" in device
    assert "ENV HUMIDITY" in device
    assert "ENV_TEMPERATURE" not in device
    assert "envTemperature" not in device


def test_payload_has_no_quality_field():
    """규격에 없는 필드를 끼워 넣지 않는다 — 백엔드가 조용히 버린다."""
    payload = build_payload(credential(), TS, seed=3)

    assert "quality" not in payload
    assert "quality" not in payload["device"]


def test_message_id_is_new_for_every_send():
    first = build_payload(credential(), TS, seed=3)["MESSAGE_ID"]
    second = build_payload(credential(), TS, seed=3)["MESSAGE_ID"]

    assert first != second


def test_device_identity_fields():
    device = device_block()

    assert device["DEVICE_ID"] == "WB-01"
    assert device["PHONE_NUMBER"] == "nothing"
    assert len(device["FW_VER"]) <= 32   # 백엔드 @Size(max = 32)


# ---- 측정 시각 -----------------------------------------------------------


def test_measured_at_is_epoch_millis_of_the_kst_instant():
    """규격은 epoch(시점)를 요구한다. KST 벽시계를 그대로 숫자로 만들지 않는다."""
    expected = int(
        TS.replace(tzinfo=timezone(timedelta(hours=9))).timestamp() * 1000
    )

    assert epoch_millis(TS) == expected
    assert device_block()["MEASURED_AT"] == expected


def test_measured_at_rejects_tz_aware_input():
    with pytest.raises(ValueError):
        epoch_millis(TS.replace(tzinfo=timezone.utc))


# ---- 혈압 ---------------------------------------------------------------


def test_systolic_always_exceeds_diastolic():
    for hour in range(24):
        for seed in range(12):
            systolic, diastolic = blood_pressure(TS.replace(hour=hour), seed)
            assert systolic > diastolic
            assert systolic - diastolic >= ation.MIN_PULSE_PRESSURE - 1


def test_blood_pressure_stays_in_plausible_band():
    values = [
        blood_pressure(TS.replace(hour=h), s) for h in range(24) for s in range(8)
    ]
    systolics = [item[0] for item in values]
    diastolics = [item[1] for item in values]

    assert 100 <= min(systolics) and max(systolics) <= 140
    assert 40 <= min(diastolics) and max(diastolics) <= 95


def test_blood_pressure_is_deterministic():
    assert blood_pressure(TS, 5) == blood_pressure(TS, 5)
    assert blood_pressure(TS, 5) != blood_pressure(TS, 6)


def test_pulse_pressure_clamp_actually_engages(monkeypatch):
    """맥압 하한이 **실제로 동작하는지**를 직접 건드려 확인한다.

    현재 파라미터에서는 두 파형의 peak_hour가 같아 간격이 32~51로 벌어져 있어
    클램프가 한 번도 발동하지 않는다 — 즉 위의 불변식 테스트는 클램프를 지워도
    통과한다(변이 검사로 확인함). 프로필을 하나만 손대도 곧바로 필요해지는
    방어선이므로, 이완기 프로필을 수축기 위로 올려 두고 그때도 불변식이
    지켜지는지 여기서 따로 본다.
    """
    runaway = ation.Profile(200.0, 0.0, 0.0, 16, 40.0, 300.0, decimals=0)
    monkeypatch.setattr(ation, "BP_DIA_PROFILE", runaway)

    for hour in range(24):
        systolic, diastolic = blood_pressure(TS.replace(hour=hour), seed=3)
        assert systolic > diastolic
        assert systolic - diastolic >= ation.MIN_PULSE_PRESSURE - 1


def test_shipped_profiles_keep_a_wide_pulse_pressure():
    """클램프에 기대지 않고도 파형 자체가 사람 범위 안에 있어야 한다.

    클램프가 상시 발동하는 상태라면 이완기가 수축기에 붙어 다니는 셈이라
    파형이 아니라 직선이 된다 — 값 모델로서는 실패다.
    """
    gaps = [
        evaluate(ation.BP_SYS_PROFILE, "bp_sys", TS.replace(hour=h), s)
        - evaluate(ation.BP_DIA_PROFILE, "bp_dia", TS.replace(hour=h), s)
        for h in range(24)
        for s in range(20)
    ]

    assert min(gaps) >= ation.MIN_PULSE_PRESSURE
    assert max(gaps) - min(gaps) > 5   # 붙어 있지 않고 실제로 움직인다


# ---- 걸음 수 -------------------------------------------------------------


def test_step_count_resets_at_midnight():
    late = step_count("WB-01", datetime(2026, 7, 20, 23, 59, 0))
    fresh = step_count("WB-01", datetime(2026, 7, 21, 0, 0, 0))

    assert fresh == 0
    assert late > 0


def test_step_count_increases_monotonically_through_the_day():
    previous = -1
    for hour in range(24):
        for minute in (0, 30):
            value = step_count("WB-01", TS.replace(hour=hour, minute=minute))
            assert value >= previous
            previous = value


def test_daily_step_total_is_in_range_and_device_specific():
    totals = {
        device: step_count(device, datetime(2026, 7, 20, 23, 59, 59))
        for device in ("WB-01", "WB-02", "WB-03", "WB-04")
    }
    for total in totals.values():
        assert 3000 <= total <= 12000
    assert len(set(totals.values())) > 1   # 기기마다 다르다


def test_night_hours_barely_add_steps():
    midnight = step_count("WB-01", datetime(2026, 7, 20, 0, 0, 0))
    dawn = step_count("WB-01", datetime(2026, 7, 20, 6, 0, 0))
    noon = step_count("WB-01", datetime(2026, 7, 20, 12, 0, 0))

    assert dawn - midnight < (noon - dawn) * 0.05


# ---- 수면 ---------------------------------------------------------------


def test_sleep_accumulates_through_the_night():
    early = sleep_minutes("WB-01", datetime(2026, 7, 20, 23, 30, 0))[0]
    middle = sleep_minutes("WB-01", datetime(2026, 7, 21, 3, 0, 0))[0]
    late = sleep_minutes("WB-01", datetime(2026, 7, 21, 6, 30, 0))[0]

    assert 0 < early < middle < late


def test_sleep_holds_previous_night_total_through_the_day():
    morning = sleep_minutes("WB-01", datetime(2026, 7, 21, 8, 0, 0))[0]
    evening = sleep_minutes("WB-01", datetime(2026, 7, 21, 20, 0, 0))[0]

    assert morning == evening
    assert 330 <= morning <= 480


def test_sleep_resets_when_the_next_session_starts():
    before = sleep_minutes("WB-01", datetime(2026, 7, 21, 22, 59, 0))[0]
    after = sleep_minutes("WB-01", datetime(2026, 7, 21, 23, 0, 0))[0]

    assert before > 0
    assert after == 0


def test_wakeup_is_a_small_share_of_sleep():
    sleep, wakeup = sleep_minutes("WB-01", datetime(2026, 7, 21, 9, 0, 0))

    assert 0.03 * sleep - 1 <= wakeup <= 0.08 * sleep + 1


# ---- 배터리 --------------------------------------------------------------


def test_battery_stays_between_floor_and_full():
    values = [
        battery_percent("WB-01", TS + timedelta(hours=hour)) for hour in range(0, 400)
    ]

    assert min(values) >= 20
    assert max(values) <= 100


def test_battery_recharges_after_reaching_the_floor():
    values = [
        battery_percent("WB-01", TS + timedelta(hours=hour)) for hour in range(0, 200)
    ]

    assert min(values) <= 25          # 바닥까지 내려간다
    assert max(values) >= 95          # 그리고 다시 찬다


def test_battery_phase_differs_per_device():
    assert battery_percent("WB-01", TS) != battery_percent("WB-02", TS)


# ---- 위치 ---------------------------------------------------------------


def test_positioning_uses_inventory_coordinates_with_jitter():
    block = positioning(credential(latitude=37.5, longitude=127.0), TS, seed=3)
    pos = block["location"]["result_data"]["pos_info"]

    assert abs(pos["latitude"] - 37.5) <= 0.0003
    assert abs(pos["longitude"] - 127.0) <= 0.0003
    assert block["location"]["result_code"] == 0
    assert 10 <= pos["accuracy_h"] <= 50
    assert pos["altitude"] == 0 and pos["floor"] == 0


def test_positioning_without_coordinates_spreads_devices_around_seoul():
    first = positioning(credential("WB-01"), TS)["location"]["result_data"]["pos_info"]
    second = positioning(credential("WB-02"), TS)["location"]["result_data"]["pos_info"]

    assert first["latitude"] != second["latitude"]
    assert abs(first["latitude"] - ation.SEOUL_LAT) <= ation.DEVICE_SPREAD_DEG + 0.001
    assert abs(first["longitude"] - ation.SEOUL_LON) <= ation.DEVICE_SPREAD_DEG + 0.001


def test_position_jitters_between_sends():
    early = positioning(credential(), TS, seed=1)["location"]["result_data"]["pos_info"]
    later = positioning(
        credential(), TS + timedelta(minutes=5), seed=1
    )["location"]["result_data"]["pos_info"]

    assert early["latitude"] != later["latitude"]


# ---- 통계 ---------------------------------------------------------------


def test_statistic_window_spans_previous_send_to_now():
    since = TS - timedelta(minutes=5)
    stat = heart_rate_statistic(TS, since, seed=3)

    assert stat["name"] == "PPG_HR"
    assert stat["window_start"] == epoch_millis(since)
    assert stat["window_end"] == epoch_millis(TS)
    assert stat["count"] == 6          # 5분 창을 60초 간격으로 샘플링
    assert stat["min"] <= stat["average"] <= stat["max"]


def test_first_send_has_a_single_sample_window():
    stat = heart_rate_statistic(TS, None, seed=3)

    assert stat["count"] == 1
    assert stat["window_start"] == stat["window_end"] == epoch_millis(TS)
    assert stat["min"] == stat["max"] == stat["average"]


def test_long_window_is_capped_to_a_sane_sample_count():
    stat = heart_rate_statistic(TS, TS - timedelta(hours=12), seed=3)

    assert stat["count"] <= ation.MAX_STAT_SAMPLES + 1


# ---- 프로파일 축 ---------------------------------------------------------


def test_bad_profile_raises_heart_rate_and_lowers_spo2():
    good = device_block(preset="good")
    bad = device_block(preset="bad")

    assert bad["PPG_HR"] > good["PPG_HR"]
    assert bad["PPG_SPO2"] <= good["PPG_SPO2"]


def test_burst_overrides_apply_to_matching_waveform_fields():
    """`ctl burst --set heart_rate=...`가 이 경로에서도 동작해야 한다."""
    plain = device_block()
    burst = device_block(overrides={"heart_rate": 160.0, "pm25": 300.0})

    assert burst["PPG_HR"] > plain["PPG_HR"]
    assert burst["PPG_HR"] <= 200.0   # 센서 프로필 상한으로 클램프


# ---- 송신 ---------------------------------------------------------------


def test_send_posts_to_ingest_path_without_auth_header():
    with requests_mock.Mocker() as mock:
        mock.post(INGEST_URL, json={"result": "success"})
        send(BASE, build_payload(credential(), TS))

        assert mock.last_request.path == "/v1/biometric/ingest"
        assert "Authorization" not in mock.last_request.headers
        assert "X-API-KEY" not in mock.last_request.headers


def test_send_tolerates_trailing_slash_in_base_url():
    with requests_mock.Mocker() as mock:
        mock.post(INGEST_URL, json={"result": "success"})
        send(f"{BASE}/", build_payload(credential(), TS))

        assert mock.last_request.path == "/v1/biometric/ingest"


def test_send_accepts_201():
    with requests_mock.Mocker() as mock:
        mock.post(INGEST_URL, status_code=201, json={"result": "success"})
        send(BASE, build_payload(credential(), TS))   # 예외가 없으면 성공


def test_forbidden_explains_the_ip_whitelist():
    """403은 기기 설정이 아니라 발신 호스트의 문제다 — 로그가 그걸 말해야 한다."""
    with requests_mock.Mocker() as mock:
        mock.post(INGEST_URL, status_code=403, json={"errorCode": "FORBIDDEN"})

        with pytest.raises(ApiError) as excinfo:
            send(BASE, build_payload(credential(), TS))

    assert excinfo.value.status == 403
    assert excinfo.value.is_rejected
    assert "ATION_ALLOWED_IPS" in str(excinfo.value)


def test_network_failure_has_no_status():
    with requests_mock.Mocker() as mock:
        mock.post(INGEST_URL, exc=requests.ConnectionError("boom"))

        with pytest.raises(ApiError) as excinfo:
            send(BASE, build_payload(credential(), TS))

    assert excinfo.value.status is None
    assert not excinfo.value.is_rejected


def test_validation_error_does_not_leak_submitted_values():
    with requests_mock.Mocker() as mock:
        mock.post(
            INGEST_URL,
            status_code=422,
            json={
                "errorCode": "VALIDATION_FAILED",
                "fieldErrors": [
                    {"field": "device.PPG_HR", "message": "범위", "rejectedValue": 9999}
                ],
            },
        )

        with pytest.raises(ApiError) as excinfo:
            send(BASE, build_payload(credential(), TS))

    assert "device.PPG_HR" in str(excinfo.value)
    assert "9999" not in str(excinfo.value)


# ---- AtionDevice ---------------------------------------------------------


def test_device_advances_the_statistic_window_between_sends():
    device = AtionDevice(credential(), BASE)
    with requests_mock.Mocker() as mock:
        mock.post(INGEST_URL, json={"result": "success"})
        device.publish(TS)
        first = mock.last_request.json()["statistics"][0]
        device.publish(TS + timedelta(minutes=5))
        second = mock.last_request.json()["statistics"][0]

    assert first["count"] == 1
    assert second["window_start"] == first["window_end"]
    assert second["count"] > 1
    assert device.sent == 2


def test_failed_send_keeps_the_window_open():
    """실패한 구간의 심박이 통계에서 사라지면 안 된다."""
    device = AtionDevice(credential(), BASE)
    with requests_mock.Mocker() as mock:
        mock.post(INGEST_URL, json={"result": "success"})
        device.publish(TS)
        mock.post(INGEST_URL, status_code=500, json={"errorCode": "OOPS"})
        with pytest.raises(ApiError):
            device.publish(TS + timedelta(minutes=5))
        mock.post(INGEST_URL, json={"result": "success"})
        device.publish(TS + timedelta(minutes=10))
        window = mock.last_request.json()["statistics"][0]

    assert window["window_start"] == epoch_millis(TS)
    assert device.sent == 2


def test_device_uses_its_profile_for_the_payload():
    device = AtionDevice(credential(), BASE, profile="very_bad")
    with requests_mock.Mocker() as mock:
        mock.post(INGEST_URL, json={"result": "success"})
        device.publish(TS)
        sent = mock.last_request.json()["device"]

    assert sent["PPG_HR"] > device_block(preset="good")["PPG_HR"]
