"""MQTT 페이로드/토픽 빌더. 실시간 발행에 필요한 것만 담는다."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from livesim.profiles import DEFAULT_PROFILE, SENSOR_PROFILES, reading

REQUIRED_FIELDS = ("device_id", "site_id", "device_type", "captured_at")

KST_OFFSET = "+09:00"
NO_OFFSET = "none"
DEFAULT_FW_VERSION = "1.0.0-sim"

# 측정 품질 플래그 — 백엔드 QualityFlag enum과 같은 값 집합이어야 한다.
# 여기서 갈리면 업로드는 성공하는데 백엔드가 알 수 없는 값으로 읽는다.
#
# 프리셋(profile)과는 **독립된 축**이다. 프리셋은 "값이 얼마나 나쁜가"이고
# 품질은 "그 값을 믿을 수 있는가"다. 그래서 품질은 측정값을 바꾸지 않는다 —
# 값은 정상인데 센서가 스스로를 신뢰할 수 없다고 보고하는 상태를 모의한다.
QUALITY_OK = "OK"
QUALITY_FLAGS = (QUALITY_OK, "DRIFT", "ERROR", "MISSING")
DEFAULT_QUALITY = QUALITY_OK


def format_captured_at(ts: datetime, offset: str = KST_OFFSET) -> str:
    """ISO-8601 문자열. offset="none"이면 오프셋 없이 반환한다.

    naive datetime만 받는다. tz-aware를 받으면 오프셋을 변환하지 않고
    +09:00을 덧붙이게 되어 실제와 다른 시각을 조용히 만들어낸다.
    """
    if ts.tzinfo is not None:
        raise ValueError(
            f"captured_at은 naive datetime이어야 합니다 (KST로 간주). 받은 값: {ts!r}"
        )
    base = ts.strftime("%Y-%m-%dT%H:%M:%S")
    return base if offset == NO_OFFSET else base + offset


def build_payload(
    device_id: str,
    site_id: str,
    device_type: str,
    ts: datetime,
    facility_type: str | None = None,
    seed: int = 0,
    captured_at_offset: str = KST_OFFSET,
    preset: str = DEFAULT_PROFILE,
    quality: str = DEFAULT_QUALITY,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "device_id": device_id,
        "site_id": site_id,
        "device_type": device_type,
        "captured_at": format_captured_at(ts, captured_at_offset),
        "schema_version": 1,
        "quality": quality,
        "fw_version": DEFAULT_FW_VERSION,
        "battery_pct": 87,
        "rssi": -62,
    }
    if facility_type:
        payload["facility_type"] = facility_type
    # facility_type은 페이로드 필드일 뿐 아니라 파형 기준이기도 하다 — 프리셋은
    # 그 시설의 법정 밴드 기준 등급이므로 시설을 모르면 등급을 맞출 수 없다.
    payload.update(reading(device_type, ts, seed, preset, facility_type))
    return payload


def clamp_to_profile(name: str, value: float) -> float:
    """센서 프로필의 min/max로 자르고 DB 컬럼 자릿수로 반올림한다.

    범위를 벗어난 값은 업로드 DTO 검증(@DecimalMin/@DecimalMax)에 걸려 422가
    나고, 그 디바이스의 그 틱이 통째로 유실된다. 시나리오가 과장된 오염
    목표값을 줘도 발행 자체는 성공해야 하므로 여기서 잘라낸다.
    """
    profile = SENSOR_PROFILES[name]
    clamped = max(profile.minimum, min(profile.maximum, float(value)))
    return round(clamped, profile.decimals)


def apply_overrides(
    payload: dict[str, Any], overrides: dict[str, float] | None
) -> dict[str, Any]:
    """오버라이드를 클램프해 덮어쓴 새 페이로드를 만든다 (원본 불변).

    페이로드에 없는 필드는 무시한다 — 디바이스 타입이 측정하지 않는 센서를
    끼워 넣으면(예: 웨어러블에 pm25) 실제 기기가 만들 수 없는 데이터가 된다.
    """
    result = dict(payload)
    if not overrides:
        return result
    for name, value in overrides.items():
        if name not in result:
            continue
        result[name] = clamp_to_profile(name, value)
    return result


def build_topic(
    facility_type: str,
    site_id: str,
    device_type: str,
    device_id: str,
    suffix: str = "sensor",
) -> str:
    """EMQX 룰이 매칭하는 6-세그먼트 토픽. device_id는 원형을 유지한다."""
    return (
        f"aiot/v1/{facility_type.lower()}/{site_id}"
        f"/{device_type.lower()}/{device_id}/{suffix}"
    )
