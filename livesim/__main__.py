"""livesim CLI.

사용법:
    python -m livesim                                  # scenarios/steady.yaml
    python -m livesim scenarios/daily-ops.yaml         # run 생략 가능 (하위 호환)
    python -m livesim run scenarios/stress.yaml --devices 10
    python -m livesim --dry-run scenarios/daily-ops.yaml
    python -m livesim ctl off AQ-GANGNAM-01
    python -m livesim ctl status
    python -m livesim rehearse
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
import traceback
from pathlib import Path

from livesim import __version__, control
from livesim.api import ApiError
from livesim.config import (
    ALERT_BURST,
    SECONDS_PER_DAY,
    ConfigError,
    Scenario,
    load_inventory,
    load_scenario,
    load_settings,
)
from livesim.panel import DEFAULT_HOST as PANEL_DEFAULT_HOST
from livesim.panel import DEFAULT_PORT as PANEL_DEFAULT_PORT
from livesim.rehearse import rehearse
from livesim.runner import RunnerError, run, select_devices

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCENARIO = ROOT / "scenarios" / "steady.yaml"
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"

SUBCOMMANDS = ("run", "ctl", "rehearse", "panel")
GLOBAL_FLAGS = ("-h", "--help", "--version")


def _fix_console_encoding() -> None:
    """Windows 콘솔은 기본 cp949라 한글/기호를 출력하면 죽는다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def normalize_argv(argv: list[str]) -> list[str]:
    """서브커맨드가 없으면 run으로 간주한다.

    0.1에서 쓰던 `python -m livesim [시나리오] [--dry-run]` 형태를 그대로
    유지하기 위한 것. 전역 플래그(--help/--version)는 최상위 파서가 받아야
    하므로 건드리지 않는다.
    """
    if argv and (argv[0] in SUBCOMMANDS or argv[0] in GLOBAL_FLAGS):
        return argv
    return ["run"] + argv


def apply_overrides(
    scenario: Scenario, devices: int | None, interval: int | None
) -> Scenario:
    """CLI 인자로 시나리오 값을 덮어쓴다 (파일은 건드리지 않는다)."""
    changes: dict[str, object] = {}
    if devices is not None:
        if devices < 0:
            raise ConfigError(f"--devices는 0 이상이어야 합니다 (받은 값: {devices})")
        changes["max_devices"] = devices
    if interval is not None:
        if interval < 1:
            raise ConfigError(f"--interval은 1 이상이어야 합니다 (받은 값: {interval})")
        changes["interval_seconds"] = interval
    return dataclasses.replace(scenario, **changes) if changes else scenario


def print_plan(scenario: Scenario, path: Path, settings, inventory) -> None:
    """접속 없이 시나리오·인벤토리 검증 결과와 발행 계획을 출력한다."""
    selected = select_devices(inventory, scenario)
    ticks_per_day = SECONDS_PER_DAY / scenario.interval_seconds
    print(f"livesim {__version__} — dry-run (API/MQTT 접속 없음)")
    print(f"시나리오 : {scenario.name}  ({path})")
    if scenario.description:
        print(f"설명     : {scenario.description}")
    print(f"인벤토리 : {settings.devices_file} — {len(inventory)}대 등록")
    print(f"발행 대상: {len(selected)}대 (제외 {len(inventory) - len(selected)}대)")
    print(
        f"발행 주기: {scenario.interval_seconds}초 "
        f"(디바이스 1대당 하루 {ticks_per_day:.0f}회)"
    )
    print(f"제어 채널: {settings.control_dir}")
    if not scenario.events:
        print("이벤트   : (없음)")
    else:
        print("이벤트   :")
        for spec in scenario.events:
            low, high = spec.duration_minutes
            line = (
                f"  - {spec.type:<12} {spec.per_device_per_day}회/대/일"
                f"  틱당 확률 {spec.start_probability(scenario.interval_seconds):.6f}"
                f"  지속 {low:g}~{high:g}분"
            )
            if spec.type == ALERT_BURST and spec.overrides:
                targets = ", ".join(
                    f"{name}={value:g}" for name, value in spec.overrides.items()
                )
                line += f"\n    오버라이드: {targets} (±10% 노이즈, 센서 범위로 클램프)"
            print(line)
    for item in selected[:5]:
        print(f"  · {item.device_id}  {item.device_type}/{item.facility_type}")
    if len(selected) > 5:
        print(f"  · ... 외 {len(selected) - 5}대")
    print("시나리오·인벤토리 검증 통과.")


def cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings()
    path = Path(args.scenario)
    scenario = apply_overrides(load_scenario(path), args.devices, args.interval)
    inventory = load_inventory(settings.devices_file)

    if args.dry_run:
        # 접속 없이 동작해야 한다 — 시나리오 lint 겸 컨테이너 점검용.
        print_plan(scenario, path, settings, inventory)
        return 0
    run(settings, scenario, inventory)
    return 0


def cmd_ctl(args: argparse.Namespace) -> int:
    settings = load_settings()
    if args.ctl_command == "status":
        return _print_status(settings.control_dir)

    minutes = getattr(args, "minutes", None)
    device_id = getattr(args, "device_id", "")
    overrides = parse_set_options(getattr(args, "set", None))
    control.write_command(
        settings.control_dir, args.ctl_command, device_id, minutes, overrides
    )
    suffix = f" ({minutes:g}분)" if minutes is not None else ""
    target = f" → {device_id}" if device_id else ""
    targets = (
        " " + ", ".join(f"{k}={v:g}" for k, v in overrides.items()) if overrides else ""
    )
    print(
        f"[ctl] {args.ctl_command}{target}{suffix}{targets} 명령을 넣었습니다 "
        f"({settings.control_dir}). 러너가 1초 내에 적용합니다."
    )
    return 0


def parse_set_options(pairs: list[str] | None) -> dict[str, float] | None:
    """`--set pm25=150`을 {"pm25": 150.0}으로. 값 검증은 control이 맡는다."""
    if not pairs:
        return None
    parsed: dict[str, object] = {}
    for item in pairs:
        name, separator, value = item.partition("=")
        if not separator or not name.strip():
            raise control.ControlError(
                f"--set은 항목=값 형태여야 합니다 (받은 값: {item!r})"
            )
        parsed[name.strip()] = value.strip()
    return control.parse_overrides(parsed)


def cmd_panel(args: argparse.Namespace) -> int:
    from livesim.panel import serve

    serve(load_settings(), args.host, args.port)
    return 0


def _print_status(control_dir: str) -> int:
    state = control.read_state(control_dir)
    print(
        f"시나리오 {state.get('scenario')} · tick {state.get('tick')} · "
        f"갱신 {state.get('updated_at')}"
    )
    header = (
        f"{'DEVICE':<22} {'TYPE':<6} {'CONN':<6} {'ONLINE':<7} {'PEND':>5}  EVENT"
    )
    print(header)
    print("-" * len(header))
    for item in state.get("devices", []):
        event = item.get("event") or "-"
        if item.get("event_manual"):
            event += " (수동)"
        # 기록 시점 값이 아니라 출력하는 지금 기준으로 다시 계산한다.
        countdown = control.format_remaining(control.remaining_seconds(item))
        if countdown:
            event += f" {countdown}"
        if item.get("disabled"):
            event = f"비활성: {item.get('disabled_reason') or '알 수 없음'}"
        print(
            f"{item.get('device_id', '?'):<22} "
            f"{control.abbreviate_type(item.get('device_type')):<6} "
            f"{'yes' if item.get('connected') else 'no':<6} "
            f"{'yes' if item.get('online') else 'no':<7} "
            f"{item.get('pending', 0):>5}  {event}"
        )
    return 0


def cmd_rehearse(args: argparse.Namespace) -> int:
    settings = load_settings()
    inventory = load_inventory(settings.devices_file)
    print(f"livesim {__version__} — 보안 리허설")
    print("모든 케이스는 '거부되어야 정상'입니다. 통과(PASS) = 제대로 막힘.\n")

    results = rehearse(settings, inventory)
    for item in results:
        mark = "PASS" if item.passed else "FAIL"
        print(f"  [{mark}] {item.case_id} {item.description} — {item.detail}")

    failed = [item for item in results if not item.passed]
    print()
    if failed:
        print(f"{len(failed)}개 케이스 실패 — 인증 경로를 점검하세요.")
        return 1
    print("전 케이스 정상 거부됨.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="livesim", description="aiot 백엔드용 24시간 디바이스 시뮬레이터"
    )
    parser.add_argument("--version", action="version", version=f"livesim {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="시나리오 실행 (기본)")
    run_parser.add_argument(
        "scenario",
        nargs="?",
        default=str(DEFAULT_SCENARIO),
        help=f"시나리오 YAML 경로 (기본: {DEFAULT_SCENARIO})",
    )
    run_parser.add_argument(
        "--devices", type=int, default=None, help="발행 디바이스 수 상한 (0=제한 없음)"
    )
    run_parser.add_argument("--interval", type=int, default=None, help="발행 주기(초)")
    run_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="접속 없이 시나리오·인벤토리를 검증하고 발행 계획만 출력",
    )
    run_parser.add_argument("--log-level", default="INFO", help="로그 레벨 (기본: INFO)")

    ctl_parser = sub.add_parser("ctl", help="실행 중인 러너에 수동 명령 전달")
    ctl_sub = ctl_parser.add_subparsers(dest="ctl_command", required=True)
    for name, help_text in (
        (control.OFF, "전원 off 모의 (발행 중단 + MQTT 해제, 버퍼링 없음)"),
        (control.ON, "재기동 (재접속 + 발행 재개)"),
    ):
        item = ctl_sub.add_parser(name, help=help_text)
        item.add_argument("device_id")
    dropout_parser = ctl_sub.add_parser(
        control.DROPOUT, help="통신 단절 (버퍼링 후 복구 시 batch 재전송)"
    )
    dropout_parser.add_argument("device_id")
    dropout_parser.add_argument(
        "--minutes", type=float, default=10.0, help="지속 시간(분, 기본 10)"
    )

    burst_parser = ctl_sub.add_parser(
        control.BURST, help="오염 급증 (기본 목표값은 시나리오 alert_burst 재사용)"
    )
    burst_parser.add_argument("device_id")
    burst_parser.add_argument(
        "--minutes", type=float, default=10.0, help="지속 시간(분, 기본 10)"
    )
    burst_parser.add_argument(
        "--set",
        action="append",
        metavar="항목=값",
        help="센서별 목표치 (반복 가능: --set pm25=150 --set co2=3000). "
             "생략하면 시나리오·내장 기본값",
    )
    ctl_sub.add_parser("status", help="state.json을 표로 출력")
    ctl_sub.add_parser(
        control.RELOAD, help="devices.yaml을 다시 읽어 플릿에 반영 (device_id 불필요)"
    )

    sub.add_parser("rehearse", help="보안 리허설 (거부되어야 정상인 3케이스)")

    panel_parser = sub.add_parser("panel", help="로컬 웹 패널 (가상 하드웨어 실험대)")
    panel_parser.add_argument(
        "--port", type=int, default=PANEL_DEFAULT_PORT,
        help=f"수신 포트 (기본 {PANEL_DEFAULT_PORT})",
    )
    panel_parser.add_argument(
        "--host", default=PANEL_DEFAULT_HOST,
        help=f"바인딩 주소 (기본 {PANEL_DEFAULT_HOST} — 로컬 전용)",
    )
    panel_parser.add_argument("--log-level", default="INFO", help="로그 레벨 (기본: INFO)")
    return parser


def main(argv: list[str] | None = None) -> int:
    _fix_console_encoding()
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(normalize_argv(raw))
    logging.basicConfig(
        level=str(getattr(args, "log_level", "INFO")).upper(),
        format=LOG_FORMAT,
        stream=sys.stdout,
    )

    try:
        if args.command == "run":
            return cmd_run(args)
        if args.command == "ctl":
            return cmd_ctl(args)
        if args.command == "rehearse":
            return cmd_rehearse(args)
        if args.command == "panel":
            return cmd_panel(args)
        return 2
    except FileNotFoundError as exc:
        print(f"[livesim] 파일을 찾을 수 없습니다: {exc}", file=sys.stderr)
        return 2
    except (ConfigError, RunnerError, ApiError, control.ControlError) as exc:
        print(f"[livesim] 중단: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"[livesim] 예기치 못한 오류: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
