import textwrap

import pytest

from livesim.config import (
    ConfigError,
    InventoryError,
    ScenarioError,
    load_inventory,
    load_scenario,
    load_settings,
)

VALID = """
name: daily-ops
description: 테스트
interval_seconds: 300
max_devices: 5
exclude_devices: [AQ-A, AQ-B]
events:
  - type: dropout
    per_device_per_day: 0.2
    duration_minutes: [5, 20]
  - type: alert_burst
    per_device_per_day: 0.1
    duration_minutes: [10, 30]
    overrides: {pm25: 120, co2: 2200}
"""


def write(tmp_path, body: str):
    path = tmp_path / "scenario.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_loads_valid_scenario(tmp_path):
    scenario = load_scenario(write(tmp_path, VALID))

    assert scenario.name == "daily-ops"
    assert scenario.interval_seconds == 300
    assert scenario.max_devices == 5
    assert scenario.exclude_devices == ("AQ-A", "AQ-B")
    assert [event.type for event in scenario.events] == ["dropout", "alert_burst"]
    assert scenario.events[0].duration_minutes == (5.0, 20.0)
    assert scenario.events[1].overrides == {"pm25": 120.0, "co2": 2200.0}


def test_defaults_are_applied(tmp_path):
    scenario = load_scenario(write(tmp_path, "name: minimal\n"))

    assert scenario.interval_seconds == 300
    assert scenario.max_devices == 0
    assert scenario.exclude_devices == ()
    assert scenario.events == ()


def test_exclude_devices_are_deduplicated(tmp_path):
    scenario = load_scenario(
        write(tmp_path, "name: t\nexclude_devices: [AQ-A, AQ-A, AQ-B]\n")
    )
    assert scenario.exclude_devices == ("AQ-A", "AQ-B")


def test_start_probability_scales_with_interval(tmp_path):
    scenario = load_scenario(write(tmp_path, VALID))
    dropout = scenario.events[0]

    assert dropout.start_probability(300) == pytest.approx(0.2 * 300 / 86400)
    # 주기를 두 배로 하면 하루 틱 수가 절반이므로 틱당 확률은 두 배가 된다.
    assert dropout.start_probability(600) == pytest.approx(
        2 * dropout.start_probability(300)
    )


def test_start_probability_is_capped_at_one(tmp_path):
    scenario = load_scenario(
        write(
            tmp_path,
            "name: t\nevents:\n  - type: dropout\n"
            "    per_device_per_day: 100000\n    duration_minutes: [1, 2]\n",
        )
    )
    assert scenario.events[0].start_probability(300) == 1.0


# ---- 검증 오류 ---------------------------------------------------------


def test_rejects_unknown_top_level_key(tmp_path):
    with pytest.raises(ScenarioError, match="알 수 없는 키"):
        load_scenario(write(tmp_path, "name: t\ncleanup: true\n"))


def test_rejects_non_mapping_root(tmp_path):
    with pytest.raises(ScenarioError, match="매핑"):
        load_scenario(write(tmp_path, "- a\n- b\n"))


def test_rejects_missing_name(tmp_path):
    with pytest.raises(ScenarioError, match="name"):
        load_scenario(write(tmp_path, "interval_seconds: 300\n"))


def test_rejects_interval_below_one(tmp_path):
    with pytest.raises(ScenarioError, match="interval_seconds"):
        load_scenario(write(tmp_path, "name: t\ninterval_seconds: 0\n"))


def test_rejects_negative_max_devices(tmp_path):
    with pytest.raises(ScenarioError, match="max_devices"):
        load_scenario(write(tmp_path, "name: t\nmax_devices: -1\n"))


def test_rejects_unknown_event_type(tmp_path):
    body = "name: t\nevents:\n  - type: explode\n    per_device_per_day: 1\n    duration_minutes: [1, 2]\n"
    with pytest.raises(ScenarioError, match="events\\[0\\].type"):
        load_scenario(write(tmp_path, body))


def test_rejects_duplicate_event_type(tmp_path):
    body = (
        "name: t\nevents:\n"
        "  - type: dropout\n    per_device_per_day: 1\n    duration_minutes: [1, 2]\n"
        "  - type: dropout\n    per_device_per_day: 2\n    duration_minutes: [1, 2]\n"
    )
    with pytest.raises(ScenarioError, match="중복"):
        load_scenario(write(tmp_path, body))


def test_rejects_alert_burst_without_overrides(tmp_path):
    body = "name: t\nevents:\n  - type: alert_burst\n    per_device_per_day: 1\n    duration_minutes: [1, 2]\n"
    with pytest.raises(ScenarioError, match="overrides가 필요"):
        load_scenario(write(tmp_path, body))


def test_rejects_overrides_on_non_alert_event(tmp_path):
    body = (
        "name: t\nevents:\n  - type: silence\n    per_device_per_day: 1\n"
        "    duration_minutes: [1, 2]\n    overrides: {pm25: 100}\n"
    )
    with pytest.raises(ScenarioError, match="alert_burst에서만"):
        load_scenario(write(tmp_path, body))


def test_rejects_unknown_sensor_in_overrides(tmp_path):
    """오타를 넘기면 그 필드는 무시돼 '켰는데 아무 일도 안 일어나는' 상태가 된다."""
    body = (
        "name: t\nevents:\n  - type: alert_burst\n    per_device_per_day: 1\n"
        "    duration_minutes: [1, 2]\n    overrides: {voc: 900}\n"
    )
    with pytest.raises(ScenarioError, match="알 수 없는 센서"):
        load_scenario(write(tmp_path, body))


def test_rejects_reversed_duration_range(tmp_path):
    body = "name: t\nevents:\n  - type: dropout\n    per_device_per_day: 1\n    duration_minutes: [20, 5]\n"
    with pytest.raises(ScenarioError, match="최소가 최대보다"):
        load_scenario(write(tmp_path, body))


def test_rejects_single_value_duration(tmp_path):
    body = "name: t\nevents:\n  - type: dropout\n    per_device_per_day: 1\n    duration_minutes: 5\n"
    with pytest.raises(ScenarioError, match="duration_minutes"):
        load_scenario(write(tmp_path, body))


def test_rejects_zero_frequency(tmp_path):
    body = "name: t\nevents:\n  - type: dropout\n    per_device_per_day: 0\n    duration_minutes: [1, 2]\n"
    with pytest.raises(ScenarioError, match="per_device_per_day"):
        load_scenario(write(tmp_path, body))


# ---- Settings ----------------------------------------------------------


def test_settings_need_no_credentials(monkeypatch):
    """0.2.0의 핵심 — 실제 디바이스는 admin 계정을 모른다."""
    for name in ("API_BASE_URL", "MQTT_HOST", "MQTT_PORT", "DEVICES_FILE", "CONTROL_DIR"):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings()

    assert settings.devices_file == "devices.yaml"
    assert settings.control_dir == "control"
    assert not hasattr(settings, "admin_username")


def test_blank_optional_vars_fall_back_to_defaults(monkeypatch):
    """docker compose의 env_file은 'KEY=' 줄을 빈 문자열로 주입한다.

    .env.example을 복사해 일부만 채우는 사용법에서, 빈 값이 기본값을 가려
    int('')로 죽으면 안 된다.
    """
    for name in ("API_BASE_URL", "MQTT_HOST", "MQTT_PORT", "DEVICES_FILE", "CONTROL_DIR"):
        monkeypatch.setenv(name, "")

    settings = load_settings()

    assert settings.api_base_url == "http://localhost:8080"
    assert settings.mqtt_host == "localhost"
    assert settings.mqtt_port == 1883
    assert settings.devices_file == "devices.yaml"
    assert settings.control_dir == "control"


def test_non_numeric_port_reports_the_offending_value(monkeypatch):
    monkeypatch.setenv("MQTT_PORT", "1883a")

    with pytest.raises(ConfigError, match="MQTT_PORT"):
        load_settings()


def test_settings_read_environment(monkeypatch):
    monkeypatch.setenv("API_BASE_URL", "http://backend:8080/")
    monkeypatch.setenv("MQTT_HOST", "emqx")
    monkeypatch.setenv("MQTT_PORT", "1884")
    monkeypatch.setenv("DEVICES_FILE", "/etc/livesim/devices.yaml")
    monkeypatch.setenv("CONTROL_DIR", "/var/run/livesim")

    settings = load_settings()

    assert settings.api_base_url == "http://backend:8080"  # 후행 슬래시 제거
    assert settings.mqtt_host == "emqx"
    assert settings.mqtt_port == 1884
    assert settings.devices_file == "/etc/livesim/devices.yaml"
    assert settings.control_dir == "/var/run/livesim"


# ---- 인벤토리 ----------------------------------------------------------

VALID_INVENTORY = """
devices:
  - device_id: AQ-01
    secret: s3cr3t-one
    site_id: 550e8400-e29b-41d4-a716-446655440000
    device_type: FIXED
    facility_type: OFFICE
  - device_id: WB-01
    secret: s3cr3t-two
    site_id: 550e8400-e29b-41d4-a716-446655440000
    device_type: wearable
    facility_type: school
"""


def write_inventory(tmp_path, body: str):
    path = tmp_path / "devices.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


def test_loads_inventory(tmp_path):
    inventory = load_inventory(write_inventory(tmp_path, VALID_INVENTORY))

    assert [item.device_id for item in inventory] == ["AQ-01", "WB-01"]
    assert inventory[0].secret == "s3cr3t-one"
    # 소문자로 적어도 백엔드 enum 형식(대문자)으로 정규화된다.
    assert inventory[1].device_type == "WEARABLE"
    assert inventory[1].facility_type == "SCHOOL"


def test_secret_is_not_in_repr(tmp_path):
    """이 객체는 로그·예외·state.json 경로를 지나므로 평문이 새면 안 된다."""
    inventory = load_inventory(write_inventory(tmp_path, VALID_INVENTORY))

    assert "s3cr3t-one" not in repr(inventory[0])
    assert "AQ-01" in repr(inventory[0])


def test_missing_inventory_file_explains_how_to_make_one(tmp_path):
    with pytest.raises(InventoryError, match="devices.example.yaml"):
        load_inventory(tmp_path / "nope.yaml")


def test_rejects_duplicate_device_id(tmp_path):
    """같은 device_id로 두 커넥션을 열면 서로를 계속 끊어낸다."""
    body = VALID_INVENTORY.replace("WB-01", "AQ-01")
    with pytest.raises(InventoryError, match="중복된 device_id"):
        load_inventory(write_inventory(tmp_path, body))


def test_rejects_blank_secret(tmp_path):
    body = VALID_INVENTORY.replace("s3cr3t-one", '""')
    with pytest.raises(InventoryError, match="secret"):
        load_inventory(write_inventory(tmp_path, body))


def test_rejects_unknown_device_type(tmp_path):
    body = VALID_INVENTORY.replace("device_type: FIXED", "device_type: DRONE")
    with pytest.raises(InventoryError, match="device_type"):
        load_inventory(write_inventory(tmp_path, body))


def test_rejects_unknown_facility_type(tmp_path):
    body = VALID_INVENTORY.replace("facility_type: OFFICE", "facility_type: FACTORY")
    with pytest.raises(InventoryError, match="facility_type"):
        load_inventory(write_inventory(tmp_path, body))


def test_rejects_unknown_entry_key(tmp_path):
    body = VALID_INVENTORY.replace(
        "    device_type: FIXED", "    password: nope\n    device_type: FIXED"
    )
    with pytest.raises(InventoryError, match="알 수 없는 키"):
        load_inventory(write_inventory(tmp_path, body))


def test_rejects_empty_device_list(tmp_path):
    with pytest.raises(InventoryError, match="devices"):
        load_inventory(write_inventory(tmp_path, "devices: []\n"))


def test_rejects_non_mapping_inventory(tmp_path):
    with pytest.raises(InventoryError, match="매핑"):
        load_inventory(write_inventory(tmp_path, "- AQ-01\n"))


def test_directory_at_inventory_path_explains_the_mount_mistake(tmp_path):
    """docker compose는 없는 파일을 마운트하면 같은 이름의 디렉터리를 만든다.

    README의 `docker compose up`을 devices.yaml 없이 먼저 실행하면 반드시 밟는다.
    """
    target = tmp_path / "devices.yaml"
    target.mkdir()

    with pytest.raises(InventoryError, match="디렉터리"):
        load_inventory(target)


def test_malformed_inventory_yaml_is_a_config_error(tmp_path):
    """손으로 고치는 파일이라 들여쓰기 실수가 트레이스백으로 나오면 안 된다."""
    with pytest.raises(InventoryError, match="YAML 문법 오류"):
        load_inventory(write_inventory(tmp_path, "devices:\n  - device_id: 'unclosed\n"))


def test_malformed_scenario_yaml_is_a_config_error(tmp_path):
    with pytest.raises(ScenarioError, match="YAML 문법 오류"):
        load_scenario(write(tmp_path, "name: [unclosed\n"))


def test_directory_at_scenario_path_explains_the_mount_mistake(tmp_path):
    target = tmp_path / "scenario.yaml"
    target.mkdir()

    with pytest.raises(ScenarioError, match="디렉터리"):
        load_scenario(target)
