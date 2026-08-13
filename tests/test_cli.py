"""CLI 서브커맨드 라우팅과 0.1 하위 호환."""

import pytest

from livesim.__main__ import build_parser, normalize_argv


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


def test_ctl_status_needs_no_device():
    args = parse(["ctl", "status"])

    assert args.ctl_command == "status"


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
