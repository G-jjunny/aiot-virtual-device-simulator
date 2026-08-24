from datetime import datetime, timedelta, timezone

import pytest

from livesim.payload import (
    REQUIRED_FIELDS,
    apply_overrides,
    build_payload,
    build_topic,
    clamp_to_profile,
    format_captured_at,
)
from livesim.profiles import SENSOR_PROFILES

TS = datetime(2026, 7, 20, 14, 30, 0)


def make():
    return build_payload(
        "SIM-001",
        "550e8400-e29b-41d4-a716-446655440001",
        "FIXED",
        TS,
        facility_type="OFFICE",
    )


def test_payload_has_all_required_fields():
    payload = make()
    for name in REQUIRED_FIELDS:
        assert name in payload, name


def test_captured_at_is_iso_with_kst_offset():
    assert make()["captured_at"] == "2026-07-20T14:30:00+09:00"


def test_captured_at_can_omit_offset():
    """livesim이 실제로 쓰는 형식 — 백엔드가 오프셋을 버리는 결함 우회."""
    payload = build_payload("SIM-001", "site", "FIXED", TS, captured_at_offset="none")
    assert payload["captured_at"] == "2026-07-20T14:30:00"


def test_facility_type_shapes_the_waveform_not_just_the_field():
    """프리셋은 그 시설의 법정 밴드 기준 등급이므로 시설이 파형에 반영돼야 한다."""
    daycare = build_payload("SIM", "s", "FIXED", TS, facility_type="DAYCARE")
    office = build_payload("SIM", "s", "FIXED", TS, facility_type="OFFICE")

    assert daycare["tvoc"] < office["tvoc"]   # 어린이집 tvoc 나쁨 경계 106
    assert daycare["pm10"] == office["pm10"]  # pm10은 양쪽 다 좋음 — 보정 없음


def test_naive_captured_at_carries_no_timezone_marker():
    formatted = format_captured_at(TS, "none")
    assert "+" not in formatted and "Z" not in formatted


def test_rejects_timezone_aware_timestamp():
    aware = TS.replace(tzinfo=timezone(timedelta(hours=9)))
    with pytest.raises(ValueError, match="naive datetime"):
        build_payload("SIM-001", "site", "FIXED", aware)


def test_payload_uses_tvoc_not_voc():
    payload = make()
    assert "tvoc" in payload
    assert "voc" not in payload


COMMON_IAQ_FIELDS = (
    "temp", "humi", "pm10", "pm25", "pm1_0", "co2", "tvoc", "noise", "odor", "lux",
)
EXTENDED_IAQ_FIELDS = ("co", "hcho", "radon", "no2", "h2s", "nh3", "o3", "o2")


def test_fixed_payload_has_common_and_extended_fields():
    payload = make()
    for name in COMMON_IAQ_FIELDS + EXTENDED_IAQ_FIELDS:
        assert name in payload, name


def test_portable_payload_has_common_but_omits_extended_fields():
    payload = build_payload("SIM-002", "site", "PORTABLE", TS)
    for name in COMMON_IAQ_FIELDS:
        assert name in payload, name
    for name in EXTENDED_IAQ_FIELDS:
        assert name not in payload, name


def test_wearable_payload_has_biometrics():
    payload = build_payload("SIM-W", "site", "WEARABLE", TS)
    assert "heart_rate" in payload
    assert "co2" not in payload


# ---- 오버라이드 --------------------------------------------------------


def test_overrides_replace_measured_value():
    payload = apply_overrides(make(), {"pm25": 120})
    assert payload["pm25"] == 120.0


def test_overrides_do_not_mutate_original():
    original = make()
    apply_overrides(original, {"pm25": 120})
    assert original["pm25"] != 120.0


def test_none_overrides_returns_equal_copy():
    original = make()
    result = apply_overrides(original, None)
    assert result == original
    assert result is not original


def test_overrides_are_clamped_to_sensor_maximum():
    """DTO 검증 범위를 넘는 목표값이 422를 유발하지 않아야 한다."""
    payload = apply_overrides(make(), {"pm25": 99999})
    assert payload["pm25"] == SENSOR_PROFILES["pm25"].maximum


def test_overrides_are_clamped_to_sensor_minimum():
    payload = apply_overrides(make(), {"co2": -500})
    assert payload["co2"] == SENSOR_PROFILES["co2"].minimum


def test_overrides_are_rounded_to_column_decimals():
    payload = apply_overrides(make(), {"hcho": 0.123456})
    assert payload["hcho"] == 0.123  # decimals=3


def test_overrides_skip_fields_the_device_does_not_measure():
    """웨어러블에 대기질 값을 끼워 넣으면 실제 기기가 만들 수 없는 데이터가 된다."""
    payload = apply_overrides(build_payload("SIM-W", "s", "WEARABLE", TS), {"pm25": 120})
    assert "pm25" not in payload


def test_clamp_to_profile_rejects_unknown_sensor():
    with pytest.raises(KeyError):
        clamp_to_profile("nope", 1.0)


# ---- 토픽 --------------------------------------------------------------


def test_topic_is_lowercase_and_ordered():
    topic = build_topic("OFFICE", "abc-123", "FIXED", "SIM-001", "sensor")
    assert topic == "aiot/v1/office/abc-123/fixed/SIM-001/sensor"


def test_batch_topic_suffix():
    topic = build_topic("SCHOOL", "s1", "PORTABLE", "SIM-002", "sensor/batch")
    assert topic.endswith("/SIM-002/sensor/batch")
