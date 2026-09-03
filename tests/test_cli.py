"""CLI 서브커맨드 라우팅과 0.1 하위 호환."""

import pytest

from livesim import control
from livesim.__main__ import (
    build_parser,
    main,
    normalize_argv,
    parse_set_options,
)
from livesim.control import ControlError


def parse(argv):
    return build_parser().parse_args(normalize_argv(argv))


# ---- 하위 호환 ---------------------------------------------------------


def test_bare_invocation_defaults_to_run():
    args = parse([])

    assert args.command == "run"
    assert args.scenario.endswith("steady.yaml")


def test_scenario_without_subcommand_still_runs():
    """0.1의 `python -m livesim scenarios/x.yaml` 형태."""
    args = parse(["scenarios/daily-ops.yaml"])

    assert args.command == "run"
    assert args.scenario == "scenarios/daily-ops.yaml"


def test_leading_flag_without_subcommand_still_runs():
    """0.1의 `python -m livesim --dry-run scenarios/x.yaml` 형태."""
    args = parse(["--dry-run", "scenarios/daily-ops.yaml"])

    assert args.command == "run"
    assert args.dry_run is True
    assert args.scenario == "scenarios/daily-ops.yaml"


def test_explicit_run_subcommand_works():
    args = parse(["run", "scenarios/stress.yaml", "--devices", "10"])

    assert args.command == "run"
    assert args.devices == 10


def test_global_flags_are_not_swallowed_by_run():
    assert normalize_argv(["--help"]) == ["--help"]
    assert normalize_argv(["--version"]) == ["--version"]


def test_subcommands_are_not_prefixed():
    assert normalize_argv(["ctl", "status"]) == ["ctl", "status"]
    assert normalize_argv(["rehearse"]) == ["rehearse"]


# ---- ctl ---------------------------------------------------------------


def test_ctl_off_requires_device_id():
    args = parse(["ctl", "off", "AQ-01"])

    assert args.command == "ctl"
    assert args.ctl_command == "off"
    assert args.device_id == "AQ-01"


def test_ctl_on_parses():
    args = parse(["ctl", "on", "AQ-01"])

    assert args.ctl_command == "on"


def test_ctl_dropout_has_default_duration():
    args = parse(["ctl", "dropout", "AQ-01"])

    assert args.ctl_command == "dropout"
    assert args.minutes == 10.0


def test_ctl_burst_accepts_minutes():
    args = parse(["ctl", "burst", "AQ-01", "--minutes", "30"])

    assert args.ctl_command == "burst"
    assert args.minutes == 30.0


def test_ctl_burst_accepts_repeated_set():
    args = parse(["ctl", "burst", "AQ-01", "--set", "pm25=150", "--set", "co2=3000"])

    assert args.set == ["pm25=150", "co2=3000"]


def test_set_options_parse_to_floats():
    assert parse_set_options(["pm25=150", "co2=3000"]) == {
        "pm25": 150.0,
        "co2": 3000.0,
    }


def test_set_options_default_to_none():
    assert parse_set_options(None) is None
    assert parse_set_options([]) is None


def test_set_option_without_equals_is_rejected():
    with pytest.raises(ControlError, match="항목=값"):
        parse_set_options(["pm25"])


def test_set_option_with_unknown_sensor_is_rejected():
    with pytest.raises(ControlError, match="알 수 없는 센서"):
        parse_set_options(["nope=1"])


def test_dropout_does_not_accept_set():
    """항목별 목표치는 버스트 전용 개념이다."""
    with pytest.raises(SystemExit):
        parse(["ctl", "dropout", "AQ-01", "--set", "pm25=150"])


def test_ctl_status_needs_no_device():
    args = parse(["ctl", "status"])

    assert args.ctl_command == "status"


def test_ctl_reload_needs_no_device():
    """플릿 전체 대상이라 device_id가 없다."""
    args = parse(["ctl", "reload"])

    assert args.ctl_command == "reload"
    assert not hasattr(args, "device_id")


def test_ctl_profile_parses():
    args = parse(["ctl", "profile", "AQ-01", "bad"])

    assert args.ctl_command == "profile"
    assert args.device_id == "AQ-01"
    assert args.preset == "bad"


def test_ctl_profile_rejects_unknown_preset():
    with pytest.raises(SystemExit):
        parse(["ctl", "profile", "AQ-01", "awful"])


def test_ctl_quality_parses():
    args = parse(["ctl", "quality", "AQ-01", "DRIFT"])

    assert args.ctl_command == "quality"
    assert args.device_id == "AQ-01"
    assert args.quality == "DRIFT"


@pytest.mark.parametrize("flag", ["OK", "DRIFT", "ERROR", "MISSING"])
def test_ctl_quality_accepts_every_backend_flag(flag):
    assert parse(["ctl", "quality", "AQ-01", flag]).quality == flag


def test_ctl_quality_rejects_unknown_flag():
    with pytest.raises(SystemExit):
        parse(["ctl", "quality", "AQ-01", "SUSPECT"])


def test_ctl_quality_rejects_lowercase_flag():
    """백엔드 enum 표기(대문자)에 맞춘다."""
    with pytest.raises(SystemExit):
        parse(["ctl", "quality", "AQ-01", "drift"])


def test_ctl_quality_has_no_duration():
    """바꿀 때까지 유지되는 상태라 기간 개념이 없다."""
    with pytest.raises(SystemExit):
        parse(["ctl", "quality", "AQ-01", "DRIFT", "--minutes", "5"])


def test_ctl_without_subcommand_is_rejected():
    with pytest.raises(SystemExit):
        parse(["ctl"])


def test_off_does_not_accept_minutes():
    """전원 off는 사람이 켤 때까지 유지된다 — 기간 개념이 없다."""
    with pytest.raises(SystemExit):
        parse(["ctl", "off", "AQ-01", "--minutes", "5"])


# ---- rehearse ----------------------------------------------------------


def test_rehearse_parses():
    assert parse(["rehearse"]).command == "rehearse"


# ---- panel -------------------------------------------------------------


def test_panel_defaults_to_loopback():
    """인증이 없으므로 기본값이 공인 바인딩이면 안 된다."""
    args = parse(["panel"])

    assert args.command == "panel"
    assert args.host == "127.0.0.1"
    assert args.port == 8390


def test_panel_accepts_host_and_port():
    args = parse(["panel", "--host", "0.0.0.0", "--port", "9000"])

    assert args.host == "0.0.0.0"
    assert args.port == 9000


def test_panel_is_a_known_subcommand():
    assert normalize_argv(["panel"]) == ["panel"]


# ---- ctl status 표 -----------------------------------------------------


def write_status(tmp_path, device):
    control.write_state(
        tmp_path,
        {
            "scenario": "t",
            "tick": 1,
            "updated_at": "2026-08-28T10:00:00",
            "devices": [device],
        },
    )


def run_status(tmp_path, monkeypatch, capsys, device):
    write_status(tmp_path, device)
    monkeypatch.setenv("CONTROL_DIR", str(tmp_path))

    assert main(["ctl", "status"]) == 0

    return capsys.readouterr().out


def test_ctl_status_shows_the_quality_column(tmp_path, monkeypatch, capsys):
    out = run_status(
        tmp_path, monkeypatch, capsys,
        {"device_id": "AQ-01", "device_type": "FIXED", "connected": True,
         "online": True, "pending": 0, "profile": "bad", "quality": "DRIFT"},
    )

    assert "QUAL" in out
    assert "DRIFT" in out


def test_ctl_status_marks_missing_quality_for_older_state_files(
    tmp_path, monkeypatch, capsys
):
    """구버전 러너가 쓴 state.json에는 quality가 없다 — 죽지 말고 '-'로."""
    out = run_status(
        tmp_path, monkeypatch, capsys,
        {"device_id": "AQ-01", "device_type": "FIXED", "connected": True,
         "online": True, "pending": 0},
    )

    assert "QUAL" in out
    assert "AQ-01" in out


# ---- ctl status — 전송 방식 -------------------------------------------


def test_ctl_status_shows_http_instead_of_yes_no_for_ation(
    tmp_path, monkeypatch, capsys
):
    """커넥션이 없는 기기에 yes/no를 쓰면 '안 붙었다'로 오독된다."""
    out = run_status(
        tmp_path, monkeypatch, capsys,
        {"device_id": "WB-ATION-1", "device_type": "WEARABLE",
         "transport": "ation_http", "connected": True, "online": True,
         "pending": 0, "quality": None},
    )

    row = [line for line in out.splitlines() if line.startswith("WB-ATION-1")][0]
    # DEVICE / TYPE / CONN / ONLINE ... — CONN 칸만 확인한다.
    assert row.split()[2] == "http"


def test_ctl_status_marks_a_failed_ation_send(tmp_path, monkeypatch, capsys):
    out = run_status(
        tmp_path, monkeypatch, capsys,
        {"device_id": "WB-ATION-1", "device_type": "WEARABLE",
         "transport": "ation_http", "connected": False, "online": True,
         "pending": 0, "quality": None},
    )

    assert "http!" in out


def test_ctl_status_keeps_yes_no_for_mqtt(tmp_path, monkeypatch, capsys):
    out = run_status(
        tmp_path, monkeypatch, capsys,
        {"device_id": "AQ-01", "device_type": "FIXED", "transport": "mqtt",
         "connected": True, "online": True, "pending": 0, "quality": "OK"},
    )

    assert "yes" in out
    assert "http" not in out


def test_ctl_status_treats_older_state_files_as_mqtt(tmp_path, monkeypatch, capsys):
    """transport가 없던 러너의 state.json도 그대로 읽혀야 한다."""
    out = run_status(
        tmp_path, monkeypatch, capsys,
        {"device_id": "AQ-01", "device_type": "FIXED", "connected": True,
         "online": True, "pending": 0},
    )

    assert "yes" in out
