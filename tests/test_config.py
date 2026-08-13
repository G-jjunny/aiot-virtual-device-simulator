import textwrap

import pytest

from livesim.config import ScenarioError, load_scenario, load_settings

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


def test_settings_require_admin_credentials(monkeypatch):
    monkeypatch.delenv("ADMIN_USERNAME", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    with pytest.raises(ScenarioError, match="ADMIN_USERNAME"):
        load_settings()


def test_blank_optional_vars_fall_back_to_defaults(monkeypatch):
    """docker compose의 env_file은 'KEY=' 줄을 빈 문자열로 주입한다.

    .env.example을 복사해 관리자 계정만 채우는 사용법에서, 빈 값이 기본값을
    가려 int('')로 죽으면 안 된다.
    """
    for name in ("API_BASE_URL", "MQTT_HOST", "MQTT_PORT"):
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("ADMIN_USERNAME", "simadmin")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")

    settings = load_settings()

    assert settings.api_base_url == "http://localhost:8080"
    assert settings.mqtt_host == "localhost"
    assert settings.mqtt_port == 1883


def test_blank_admin_credentials_are_treated_as_missing(monkeypatch):
    monkeypatch.setenv("ADMIN_USERNAME", "   ")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")

    with pytest.raises(ScenarioError, match="ADMIN_USERNAME"):
        load_settings()


def test_non_numeric_port_reports_the_offending_value(monkeypatch):
    monkeypatch.setenv("MQTT_PORT", "1883a")
    monkeypatch.setenv("ADMIN_USERNAME", "simadmin")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")

    with pytest.raises(ScenarioError, match="MQTT_PORT"):
        load_settings()


def test_settings_read_environment(monkeypatch):
    monkeypatch.setenv("API_BASE_URL", "http://backend:8080/")
    monkeypatch.setenv("MQTT_HOST", "emqx")
    monkeypatch.setenv("MQTT_PORT", "1884")
    monkeypatch.setenv("ADMIN_USERNAME", "simadmin")
    monkeypatch.setenv("ADMIN_PASSWORD", "secret")

    settings = load_settings()

    assert settings.api_base_url == "http://backend:8080"  # 후행 슬래시 제거
    assert settings.mqtt_host == "emqx"
    assert settings.mqtt_port == 1884
    assert settings.admin_username == "simadmin"
