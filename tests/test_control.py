import json

import pytest

from livesim import control


def test_write_then_drain_roundtrip(tmp_path):
    control.write_command(tmp_path, control.DROPOUT, "AQ-01", minutes=15)

    commands = control.drain_commands(tmp_path)

    assert len(commands) == 1
    assert commands[0] == control.Command(control.DROPOUT, "AQ-01", 15.0)


def test_drain_removes_command_files(tmp_path):
    """같은 명령이 매 폴링마다 다시 적용되면 안 된다."""
    control.write_command(tmp_path, control.OFF, "AQ-01")

    control.drain_commands(tmp_path)

    assert control.drain_commands(tmp_path) == []
    assert list(tmp_path.glob("cmd-*.json")) == []


def test_commands_drain_in_issue_order(tmp_path):
    for device_id in ("AQ-01", "AQ-02", "AQ-03"):
        control.write_command(tmp_path, control.OFF, device_id)

    commands = control.drain_commands(tmp_path)

    assert [item.device_id for item in commands] == ["AQ-01", "AQ-02", "AQ-03"]


def test_missing_minutes_is_none(tmp_path):
    control.write_command(tmp_path, control.OFF, "AQ-01")

    assert control.drain_commands(tmp_path)[0].minutes is None


def test_broken_command_file_is_dropped_not_fatal(tmp_path):
    """깨진 명령 하나가 제어 채널 전체를 막으면 안 된다."""
    (tmp_path / "cmd-broken.json").write_text("{not json", encoding="utf-8")
    control.write_command(tmp_path, control.ON, "AQ-02")

    commands = control.drain_commands(tmp_path)

    assert [item.device_id for item in commands] == ["AQ-02"]
    assert list(tmp_path.glob("cmd-*.json")) == []


def test_unknown_command_in_file_is_dropped(tmp_path):
    (tmp_path / "cmd-evil.json").write_text(
        json.dumps({"command": "rm-rf", "device_id": "AQ-01"}), encoding="utf-8"
    )

    assert control.drain_commands(tmp_path) == []


def test_write_rejects_unknown_command(tmp_path):
    with pytest.raises(control.ControlError, match="알 수 없는 명령"):
        control.write_command(tmp_path, "explode", "AQ-01")


def test_drain_on_missing_directory_is_empty(tmp_path):
    assert control.drain_commands(tmp_path / "nope") == []


def test_command_file_appears_atomically(tmp_path):
    """러너가 1초마다 폴링하므로 쓰다 만 파일이 보이면 안 된다."""
    control.write_command(tmp_path, control.BURST, "AQ-01", minutes=5)

    visible = list(tmp_path.glob("cmd-*.json"))
    assert len(visible) == 1
    assert json.loads(visible[0].read_text(encoding="utf-8"))["command"] == "burst"


def test_state_roundtrip(tmp_path):
    control.write_state(tmp_path, {"tick": 3, "devices": []})

    assert control.read_state(tmp_path)["tick"] == 3


def test_read_state_without_runner_explains_why(tmp_path):
    with pytest.raises(control.ControlError, match="러너가 실행 중인지"):
        control.read_state(tmp_path)


def test_corrupt_state_file_is_a_control_error_not_a_traceback(tmp_path):
    """깨진 상태 파일에 트레이스백을 뱉으면 ctl status가 진단을 방해한다."""
    (tmp_path / control.STATE_FILE).write_text("{truncated", encoding="utf-8")

    with pytest.raises(control.ControlError, match="읽을 수 없습니다"):
        control.read_state(tmp_path)


def test_state_file_with_bom_is_readable(tmp_path):
    """사람이 손으로 열어보거나 다른 도구로 만든 파일에 BOM이 붙을 수 있다."""
    (tmp_path / control.STATE_FILE).write_text(
        '{"tick": 7}', encoding="utf-8-sig"
    )

    assert control.read_state(tmp_path)["tick"] == 7


def test_command_file_with_bom_is_readable(tmp_path):
    (tmp_path / "cmd-1.json").write_text(
        '{"command": "on", "device_id": "AQ-01"}', encoding="utf-8-sig"
    )

    assert control.drain_commands(tmp_path)[0].device_id == "AQ-01"


def test_state_write_creates_directory(tmp_path):
    target = tmp_path / "nested" / "control"

    control.write_state(target, {"tick": 0})

    assert (target / control.STATE_FILE).exists()


# ---- 남은시간 계산 -----------------------------------------------------


def test_remaining_is_recomputed_from_absolute_end_time():
    """state.json이 5분 전에 쓰였어도 지금 기준 잔여가 나와야 한다."""
    device = {"event_ends_at": 1000.0, "event_ends_in": 300.0}

    assert control.remaining_seconds(device, now=800.0) == 200.0
    assert control.remaining_seconds(device, now=950.0) == 50.0


def test_remaining_never_goes_negative():
    assert control.remaining_seconds({"event_ends_at": 1000.0}, now=1200.0) == 0.0


def test_remaining_falls_back_to_frozen_value():
    """구버전 러너가 쓴 state.json에는 절대 종료시각이 없다."""
    assert control.remaining_seconds({"event_ends_in": 42.0}, now=999.0) == 42.0


def test_remaining_is_none_without_an_event():
    assert control.remaining_seconds({}, now=1.0) is None
    assert control.remaining_seconds({"event_ends_in": None}, now=1.0) is None


def test_format_remaining_uses_mmss():
    assert control.format_remaining(125.0) == "02:05 남음"
    assert control.format_remaining(59.9) == "00:59 남음"


def test_format_remaining_explains_the_wait_at_zero():
    """스케줄러는 틱 경계에서만 이벤트를 걷는다 — 0이어도 바로 안 사라진다."""
    assert "다음 틱" in control.format_remaining(0.0)
    assert "다음 틱" in control.format_remaining(-5.0)


def test_format_remaining_is_empty_without_an_event():
    assert control.format_remaining(None) == ""


# ---- 명령 overrides ----------------------------------------------------


def test_overrides_survive_a_write_read_roundtrip(tmp_path):
    control.write_command(
        tmp_path, control.BURST, "AQ-01", minutes=60,
        overrides={"pm25": 150, "co2": 3000},
    )

    command = control.drain_commands(tmp_path)[0]

    assert command.minutes == 60.0
    assert command.overrides == {"pm25": 150.0, "co2": 3000.0}


def test_command_without_overrides_reads_as_none(tmp_path):
    """필드가 없던 예전 명령 파일도 그대로 읽혀야 한다."""
    (tmp_path / "cmd-old.json").write_text(
        json.dumps({"command": "burst", "device_id": "AQ-01", "minutes": 10}),
        encoding="utf-8",
    )

    assert control.drain_commands(tmp_path)[0].overrides is None


def test_write_rejects_unknown_sensor(tmp_path):
    with pytest.raises(control.ControlError, match="알 수 없는 센서"):
        control.write_command(tmp_path, control.BURST, "AQ-01", overrides={"nope": 1})

    assert list(tmp_path.glob("cmd-*.json")) == []


def test_write_rejects_non_numeric_target(tmp_path):
    with pytest.raises(control.ControlError, match="숫자"):
        control.write_command(
            tmp_path, control.BURST, "AQ-01", overrides={"pm25": "높게"}
        )


def test_parse_overrides_accepts_numeric_strings():
    """--set pm25=150은 문자열로 들어온다."""
    assert control.parse_overrides({"pm25": "150"}) == {"pm25": 150.0}


def test_parse_overrides_rejects_booleans():
    with pytest.raises(control.ControlError, match="숫자"):
        control.parse_overrides({"pm25": True})


def test_parse_overrides_passes_through_none_and_empty():
    assert control.parse_overrides(None) is None
    assert control.parse_overrides({}) is None


def test_parse_overrides_rejects_non_mapping():
    with pytest.raises(control.ControlError, match="매핑"):
        control.parse_overrides([("pm25", 1)])


# ---- 환경 프로파일 명령 --------------------------------------------------


def test_profile_command_roundtrip(tmp_path):
    control.write_command(tmp_path, control.PROFILE, "AQ-01", preset="bad")

    command = control.drain_commands(tmp_path)[0]

    assert command.command == "profile"
    assert command.device_id == "AQ-01"
    assert command.preset == "bad"


def test_site_scoped_profile_command_roundtrip(tmp_path):
    control.write_command(tmp_path, control.PROFILE, site_id="S-1", preset="very_bad")

    command = control.drain_commands(tmp_path)[0]

    assert command.site_id == "S-1"
    assert command.device_id == ""
    assert command.preset == "very_bad"


def test_profile_command_requires_a_target(tmp_path):
    with pytest.raises(control.ControlError, match="device_id 또는 site_id"):
        control.write_command(tmp_path, control.PROFILE, preset="bad")


def test_profile_command_rejects_unknown_preset(tmp_path):
    with pytest.raises(control.ControlError, match="환경 프리셋"):
        control.write_command(tmp_path, control.PROFILE, "AQ-01", preset="awful")

    assert list(tmp_path.glob("cmd-*.json")) == []


def test_older_command_without_profile_fields_still_reads(tmp_path):
    (tmp_path / "cmd-old.json").write_text(
        json.dumps({"command": "off", "device_id": "AQ-01"}), encoding="utf-8"
    )

    command = control.drain_commands(tmp_path)[0]

    assert command.preset == ""
    assert command.site_id == ""


# ---- 유형 축약 ---------------------------------------------------------


def test_abbreviate_type_fits_the_column():
    assert control.abbreviate_type("FIXED") == "FIXED"
    assert control.abbreviate_type("PORTABLE") == "PORT"
    assert control.abbreviate_type("WEARABLE") == "WEAR"


def test_abbreviate_type_handles_missing_and_unknown():
    """구버전 러너가 쓴 state.json에는 device_type이 없다."""
    assert control.abbreviate_type(None) == "-"
    assert control.abbreviate_type("") == "-"
    assert control.abbreviate_type("SATELLITE") == "SATEL"
