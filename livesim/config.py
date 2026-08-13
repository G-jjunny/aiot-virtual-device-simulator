"""환경변수 Settings, 디바이스 크리덴셜 인벤토리, 시나리오 YAML 로딩/검증.

DB 접속 정보는 일부러 없다. 이 시뮬레이터는 REST API와 MQTT만으로 동작한다.

0.2.0부터 관리자 계정에도 의존하지 않는다. 실제 디바이스는 admin 자격증명을
알지 못한 채 공장/설치 시 주입받은 device_id와 secret만으로 동작하므로,
시뮬레이터도 같은 조건에 둔다. 사이트·디바이스 등록과 시크릿 발급은 관리자가
FE 대시보드에서 수행하고, 그 결과를 devices.yaml에 옮겨 적는 것이 주입에 해당한다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from livesim.profiles import DEVICE_FIELDS, SENSOR_PROFILES


class ConfigError(ValueError):
    """설정(환경변수/시나리오/인벤토리)이 잘못되었을 때의 공통 상위 예외."""


class ScenarioError(ConfigError):
    """시나리오 YAML이 잘못되었을 때 발생."""


class InventoryError(ConfigError):
    """디바이스 인벤토리(devices.yaml)가 잘못되었을 때 발생."""


DROPOUT = "dropout"
SILENCE = "silence"
ALERT_BURST = "alert_burst"
EVENT_TYPES = (DROPOUT, SILENCE, ALERT_BURST)

POWER_OFF = "power_off"
"""수동 전용 상태 — 시나리오 YAML에는 쓸 수 없다.

확률 이벤트는 "언젠가 저절로 끝나는" 장애를 모의하지만, 전원 차단은 사람이
다시 켤 때까지 유지되어야 하므로 스케줄에 섞지 않는다.
"""

MIN_INTERVAL_SECONDS = 1
SECONDS_PER_DAY = 86400

# 인벤토리 검증용. device_type은 발행 필드를 결정하는 프로필 키와 같아야 한다 —
# 여기서 갈리면 "등록은 됐는데 측정값이 하나도 안 실리는" 디바이스가 생긴다.
DEVICE_TYPES = tuple(DEVICE_FIELDS)
FACILITY_TYPES = (
    "OFFICE", "SCHOOL", "DAYCARE", "WELFARE", "HOME", "HOME_ELDERLY",
)

_SCENARIO_KEYS = {
    "name", "description", "interval_seconds", "max_devices",
    "exclude_devices", "events",
}
_EVENT_KEYS = {"type", "per_device_per_day", "duration_minutes", "overrides"}


def _check_keys(
    where: str,
    data: dict[str, Any],
    allowed: set[str],
    error: type[ConfigError] = ScenarioError,
) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise error(f"{where}: 알 수 없는 키 {sorted(unknown)}")


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
    raw = _read_yaml(Path(path), ScenarioError, "시나리오")
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
class DeviceCredential:
    """디바이스 1대에 주입된 자격증명과 소속 정보.

    secret은 repr에서 제외한다 — 이 객체는 로그·예외·state.json 경로를 두루
    지나므로, 기본 repr에 평문이 들어가면 어딘가에는 반드시 남는다.
    """

    device_id: str
    secret: str = field(repr=False)
    site_id: str
    device_type: str
    facility_type: str


_INVENTORY_KEYS = {
    "device_id", "secret", "site_id", "device_type", "facility_type",
}


def _inventory_text(where: str, entry: dict[str, Any], key: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise InventoryError(f"{where}.{key}: 비어 있지 않은 문자열이어야 합니다")
    return value.strip()


def _inventory_enum(
    where: str, entry: dict[str, Any], key: str, allowed: tuple[str, ...]
) -> str:
    value = _inventory_text(where, entry, key).upper()
    if value not in allowed:
        raise InventoryError(
            f"{where}.{key}: {allowed} 중 하나여야 합니다 (받은 값: {value!r})"
        )
    return value


def _read_yaml(path: Path, error: type[ConfigError], what: str) -> Any:
    """YAML을 읽어 파싱한다. 읽기·문법 오류를 전부 설정 오류로 바꾼다.

    사람이 손으로 고치는 파일이므로, 들여쓰기 실수나 마운트 사고가 트레이스백이
    아니라 고칠 방법이 담긴 한 줄로 나와야 한다.
    """
    if path.is_dir():
        raise error(
            f"{what} 경로가 디렉터리입니다: {path}\n"
            "docker compose가 존재하지 않는 파일을 볼륨으로 마운트하면 같은 이름의 "
            "디렉터리를 만듭니다. 이 디렉터리를 지우고 파일로 다시 만든 뒤 실행하세요."
        )
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise error(f"{what}를 읽을 수 없습니다 ({path}): {exc}") from exc
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise error(f"{what} YAML 문법 오류 ({path}): {exc}") from exc


def load_inventory(path: str | Path) -> tuple[DeviceCredential, ...]:
    """devices.yaml을 읽어 디바이스 자격증명 목록을 만든다."""
    path = Path(path)
    if not path.is_dir() and not path.exists():
        raise InventoryError(
            f"디바이스 인벤토리 파일이 없습니다: {path}\n"
            "FE 관리자 화면에서 디바이스를 등록하고 시크릿을 발급받은 뒤, "
            "devices.example.yaml을 참고해 devices.yaml을 만드세요."
        )

    raw = _read_yaml(path, InventoryError, "인벤토리")
    if not isinstance(raw, dict):
        raise InventoryError("인벤토리 최상위는 매핑이어야 합니다 (devices: [...])")
    unknown = set(raw) - {"devices"}
    if unknown:
        raise InventoryError(f"인벤토리: 알 수 없는 키 {sorted(unknown)}")

    entries = raw.get("devices")
    if not isinstance(entries, list) or not entries:
        raise InventoryError("devices: 비어 있지 않은 목록이어야 합니다")

    credentials: list[DeviceCredential] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        where = f"devices[{index}]"
        if not isinstance(entry, dict):
            raise InventoryError(f"{where}: 매핑이어야 합니다")
        _check_keys(where, entry, _INVENTORY_KEYS, InventoryError)

        device_id = _inventory_text(where, entry, "device_id")
        if device_id in seen:
            # 같은 device_id로 두 커넥션을 열면 EMQX가 먼저 붙은 쪽을 끊는다
            # (같은 client_id 재접속). 두 디바이스가 서로를 계속 밀어낸다.
            raise InventoryError(f"devices: 중복된 device_id '{device_id}'")
        seen.add(device_id)

        credentials.append(
            DeviceCredential(
                device_id=device_id,
                secret=_inventory_text(where, entry, "secret"),
                site_id=_inventory_text(where, entry, "site_id"),
                device_type=_inventory_enum(where, entry, "device_type", DEVICE_TYPES),
                facility_type=_inventory_enum(
                    where, entry, "facility_type", FACILITY_TYPES
                ),
            )
        )
    return tuple(credentials)


@dataclass(frozen=True)
class Settings:
    api_base_url: str
    mqtt_host: str
    mqtt_port: int
    devices_file: str
    control_dir: str


def _optional(name: str, default: str) -> str:
    """빈 값은 미설정으로 본다.

    docker compose의 env_file은 'KEY=' 줄을 빈 문자열로 주입한다. .env.example을
    복사해 일부만 채우는 흔한 사용법에서 os.getenv가 기본값 대신 ''를 돌려주므로,
    기본값이 있는 항목은 빈 값을 미설정과 동일하게 다룬다.
    """
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def load_settings() -> Settings:
    port = _optional("MQTT_PORT", "1883")
    try:
        mqtt_port = int(port)
    except ValueError:
        raise ConfigError(f"MQTT_PORT는 정수여야 합니다 (받은 값: {port!r})") from None

    return Settings(
        api_base_url=_optional("API_BASE_URL", "http://localhost:8080").rstrip("/"),
        mqtt_host=_optional("MQTT_HOST", "localhost"),
        mqtt_port=mqtt_port,
        devices_file=_optional("DEVICES_FILE", "devices.yaml"),
        control_dir=_optional("CONTROL_DIR", "control"),
    )
