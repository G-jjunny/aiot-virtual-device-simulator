"""livesim CLI.

사용법:
    python -m livesim                                  # scenarios/steady.yaml
    python -m livesim scenarios/daily-ops.yaml
    python -m livesim scenarios/stress.yaml --devices 10 --interval 60
    python -m livesim scenarios/daily-ops.yaml --dry-run
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
import traceback
from pathlib import Path

from livesim import __version__
from livesim.api import ApiError
from livesim.config import (
    ALERT_BURST,
    SECONDS_PER_DAY,
    Scenario,
    ScenarioError,
    load_scenario,
    load_settings,
)
from livesim.runner import RunnerError, run

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCENARIO = ROOT / "scenarios" / "steady.yaml"
LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"


def _fix_console_encoding() -> None:
    """Windows 콘솔은 기본 cp949라 한글/기호를 출력하면 죽는다."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def apply_overrides(scenario: Scenario, devices: int | None, interval: int | None) -> Scenario:
    """CLI 인자로 시나리오 값을 덮어쓴다 (파일은 건드리지 않는다)."""
    changes: dict[str, object] = {}
    if devices is not None:
        if devices < 0:
            raise ScenarioError(f"--devices는 0 이상이어야 합니다 (받은 값: {devices})")
        changes["max_devices"] = devices
    if interval is not None:
        if interval < 1:
            raise ScenarioError(f"--interval은 1 이상이어야 합니다 (받은 값: {interval})")
        changes["interval_seconds"] = interval
    return dataclasses.replace(scenario, **changes) if changes else scenario


def print_plan(scenario: Scenario, path: Path) -> None:
    """접속 없이 시나리오 검증 결과와 발행 계획을 출력한다."""
    ticks_per_day = SECONDS_PER_DAY / scenario.interval_seconds
    print(f"livesim {__version__} — dry-run (API/MQTT 접속 없음)")
    print(f"시나리오 : {scenario.name}  ({path})")
    if scenario.description:
        print(f"설명     : {scenario.description}")
    print(
        f"발행 주기: {scenario.interval_seconds}초 "
        f"(디바이스 1대당 하루 {ticks_per_day:.0f}회)"
    )
    print(
        "디바이스 : "
        + ("제한 없음 (MAINTENANCE 제외 전체)" if scenario.max_devices == 0
           else f"최대 {scenario.max_devices}대")
    )
    print(
        "제외     : "
        + (", ".join(scenario.exclude_devices) if scenario.exclude_devices else "(없음)")
    )
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
    print("시나리오 검증 통과.")


def main() -> int:
    _fix_console_encoding()
    parser = argparse.ArgumentParser(
        prog="livesim", description="aiot 백엔드용 24시간 디바이스 시뮬레이터"
    )
    parser.add_argument(
        "scenario",
        nargs="?",
        default=str(DEFAULT_SCENARIO),
        help=f"시나리오 YAML 경로 (기본: {DEFAULT_SCENARIO})",
    )
    parser.add_argument(
        "--devices", type=int, default=None, help="발행 디바이스 수 상한 (0=제한 없음)"
    )
    parser.add_argument("--interval", type=int, default=None, help="발행 주기(초)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="접속 없이 시나리오 검증과 발행 계획만 출력하고 종료",
    )
    parser.add_argument(
        "--log-level", default="INFO", help="로그 레벨 (기본: INFO)"
    )
    parser.add_argument("--version", action="version", version=f"livesim {__version__}")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(), format=LOG_FORMAT, stream=sys.stdout
    )

    try:
        path = Path(args.scenario)
        scenario = apply_overrides(
            load_scenario(path), args.devices, args.interval
        )
        if args.dry_run:
            # 접속 정보 없이도 동작해야 한다 — 시나리오 lint 겸 컨테이너 점검용.
            print_plan(scenario, path)
            return 0
        run(load_settings(), scenario)
        return 0
    except FileNotFoundError as exc:
        print(f"[livesim] 시나리오 파일을 찾을 수 없습니다: {exc}", file=sys.stderr)
        return 2
    except (ScenarioError, RunnerError, ApiError) as exc:
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
