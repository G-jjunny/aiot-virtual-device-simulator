"""환경변수 Settings와 시나리오 YAML 로딩/검증.

DB 접속 정보는 일부러 없다. 이 시뮬레이터는 REST API와 MQTT만으로 동작하며,
DB 스키마를 직접 알지 못한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from livesim.profiles import SENSOR_PROFILES


class ScenarioError(ValueError):
    """시나리오 YAML 또는 환경변수가 잘못되었을 때 발생."""


DROPOUT = "dropout"
SILENCE = "silence"
ALERT_BURST = "alert_burst"
EVENT_TYPES = (DROPOUT, SILENCE, ALERT_BURST)

MIN_INTERVAL_SECONDS = 1
SECONDS_PER_DAY = 86400

_SCENARIO_KEYS = {
    "name", "description", "interval_seconds", "max_devices",
    "exclude_devices", "events",
}
_EVENT_KEYS = {"type", "per_device_per_day", "duration_minutes", "overrides"}


def _check_keys(where: str, data: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ScenarioError(f"{where}: 알 수 없는 키 {sorted(unknown)}")


@dataclass(frozen=True)
class EventSpec:
    type: str
    per_device_per_day: float
    duration_minutes: tuple[float, float]
    overrides: dict[str, float] | None = None

    def start_probability(self, interval_seconds: int) -> float:
        """틱 1회당 이벤트 시작 확률.

        하루 기대 발생 횟수를 하루 틱 수로 나눈 값. 주기를 바꾸면 확률이
        따라 움직여야 "하루 N회"라는 시나리오 의미가 유지된다.
        """
        probability = self.per_device_per_day * interval_seconds / SECONDS_PER_DAY
        return min(1.0, probability)


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    interval_seconds: int
    max_devices: int
    exclude_devices: tuple[str, ...]
    events: tuple[EventSpec, ...]


def _positive_number(where: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScenarioError(f"{where}: 숫자여야 합니다 (받은 값: {value!r})")
    if value <= 0:
        raise ScenarioError(f"{where}: 0보다 커야 합니다 (받은 값: {value!r})")
    return float(value)


def _parse_duration(where: str, raw: Any) -> tuple[float, float]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ScenarioError(
            f"{where}.duration_minutes: [최소, 최대] 형태의 2개 값이어야 합니다 "
            f"(받은 값: {raw!r})"
        )
    low = _positive_number(f"{where}.duration_minutes[0]", raw[0])
    high = _positive_number(f"{where}.duration_minutes[1]", raw[1])
    if low > high:
        raise ScenarioError(
            f"{where}.duration_minutes: 최소가 최대보다 큽니다 ({low} > {high})"
        )
    return (low, high)


def _parse_overrides(where: str, raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise ScenarioError(f"{where}.overrides: 비어 있지 않은 매핑이어야 합니다")
    parsed: dict[str, float] = {}
    for name, value in raw.items():
        if name not in SENSOR_PROFILES:
            # 오타를 조용히 넘기면 그 필드는 그냥 무시돼, 시나리오를 켜도
            # 아무 일도 일어나지 않는 상태를 디버깅하게 된다.
            raise ScenarioError(
                f"{where}.overrides: 알 수 없는 센서 '{name}' "
                f"(사용 가능: {', '.join(sorted(SENSOR_PROFILES))})"
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ScenarioError(f"{where}.overrides.{name}: 숫자여야 합니다")
        parsed[name] = float(value)
    return parsed


def _parse_event(index: int, raw: Any) -> EventSpec:
    where = f"events[{index}]"
    if not isinstance(raw, dict):
        raise ScenarioError(f"{where}: 매핑이어야 합니다")
    _check_keys(where, raw, _EVENT_KEYS)

    event_type = raw.get("type")
    if event_type not in EVENT_TYPES:
        raise ScenarioError(
            f"{where}.type: {EVENT_TYPES} 중 하나여야 합니다 (받은 값: {event_type!r})"
        )

    overrides_raw = raw.get("overrides")
    if event_type == ALERT_BURST:
        if overrides_raw is None:
            raise ScenarioError(f"{where}: alert_burst에는 overrides가 필요합니다")
        overrides = _parse_overrides(where, overrides_raw)
    else:
        if overrides_raw is not None:
            raise ScenarioError(
                f"{where}: overrides는 alert_burst에서만 쓸 수 있습니다 "
                f"(type={event_type})"
            )
        overrides = None

    return EventSpec(
        type=event_type,
        per_device_per_day=_positive_number(
            f"{where}.per_device_per_day", raw.get("per_device_per_day")
        ),
        duration_minutes=_parse_duration(where, raw.get("duration_minutes")),
        overrides=overrides,
    )


def load_scenario(path: str | Path) -> Scenario:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ScenarioError("시나리오 최상위는 매핑이어야 합니다")
    _check_keys("시나리오", raw, _SCENARIO_KEYS)

    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ScenarioError("name은 비어 있지 않은 문자열이어야 합니다")

    interval = raw.get("interval_seconds", 300)
    if isinstance(interval, bool) or not isinstance(interval, int):
        raise ScenarioError(
            f"interval_seconds는 정수여야 합니다 (받은 값: {interval!r})"
        )
    if interval < MIN_INTERVAL_SECONDS:
        # 1초 미만이면 두 틱의 captured_at이 같아져 뒤 발행이 앞 행을 덮어쓴다.
        raise ScenarioError(
            f"interval_seconds는 {MIN_INTERVAL_SECONDS} 이상이어야 합니다 "
            f"(받은 값: {interval})"
        )

    max_devices = raw.get("max_devices", 0)
    if isinstance(max_devices, bool) or not isinstance(max_devices, int):
        raise ScenarioError(f"max_devices는 정수여야 합니다 (받은 값: {max_devices!r})")
    if max_devices < 0:
        raise ScenarioError(f"max_devices는 0 이상이어야 합니다 (받은 값: {max_devices})")

    excluded_raw = raw.get("exclude_devices") or []
    if not isinstance(excluded_raw, list):
        raise ScenarioError("exclude_devices는 목록이어야 합니다")
    excluded: list[str] = []
    for item in excluded_raw:
        if not isinstance(item, str) or not item.strip():
            raise ScenarioError(
                f"exclude_devices: 비어 있지 않은 문자열이어야 합니다 (받은 값: {item!r})"
            )
        if item not in excluded:
            excluded.append(item)

    events_raw = raw.get("events") or []
    if not isinstance(events_raw, list):
        raise ScenarioError("events는 목록이어야 합니다")
    events = [_parse_event(index, item) for index, item in enumerate(events_raw)]
    seen_types = {event.type for event in events}
    if len(seen_types) != len(events):
        # 한 디바이스는 동시에 하나의 이벤트만 갖는다. 같은 타입이 둘이면
        # 뒤쪽 정의는 앞쪽에 가려 사실상 무시된다.
        raise ScenarioError("events: 같은 type이 중복되었습니다")

    return Scenario(
        name=name,
        description=str(raw.get("description", "")),
        interval_seconds=interval,
        max_devices=max_devices,
        exclude_devices=tuple(excluded),
        events=tuple(events),
    )


@dataclass(frozen=True)
class Settings:
    api_base_url: str
    mqtt_host: str
    mqtt_port: int
    admin_username: str
    admin_password: str


def _required(name: str) -> str:
    value = os.getenv(name)
    if not value or not value.strip():
        raise ScenarioError(f"환경변수 {name}가 필요합니다")
    return value.strip()


def _optional(name: str, default: str) -> str:
    """빈 값은 미설정으로 본다.

    docker compose의 env_file은 'KEY=' 줄을 빈 문자열로 주입한다. .env.example을
    복사해 관리자 계정만 채우는 흔한 사용법에서 os.getenv가 기본값 대신 ''를
    돌려주므로, 기본값이 있는 항목은 빈 값을 미설정과 동일하게 다룬다.
    """
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def load_settings() -> Settings:
    port = _optional("MQTT_PORT", "1883")
    try:
        mqtt_port = int(port)
    except ValueError:
        raise ScenarioError(f"MQTT_PORT는 정수여야 합니다 (받은 값: {port!r})") from None

    return Settings(
        api_base_url=_optional("API_BASE_URL", "http://localhost:8080").rstrip("/"),
        mqtt_host=_optional("MQTT_HOST", "localhost"),
        mqtt_port=mqtt_port,
        admin_username=_required("ADMIN_USERNAME"),
        admin_password=_required("ADMIN_PASSWORD"),
    )
