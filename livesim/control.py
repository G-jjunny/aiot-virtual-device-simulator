"""파일 기반 제어 채널 — 실행 중인 러너에 개별 디바이스 조작 명령을 넣는다.

디렉터리 하나를 공유하는 것으로 충분해서 TCP/HTTP 서버를 두지 않는다. 러너에
포트를 열면 인증·바인딩 주소·방화벽을 전부 따져야 하는데, 이 도구가 필요한 건
"같은 호스트(또는 같은 볼륨)에서 사람이 가끔 명령을 넣는" 것뿐이다.

명령 파일은 임시 이름으로 쓴 뒤 rename한다. 러너가 1초마다 폴링하므로, 쓰는
도중의 파일을 읽어 JSON 파싱에 실패하는 경우를 없애려면 원자적 등장이 필요하다.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LOG = logging.getLogger("livesim.control")

COMMAND_PREFIX = "cmd-"
COMMAND_SUFFIX = ".json"
STATE_FILE = "state.json"

OFF = "off"
ON = "on"
DROPOUT = "dropout"
BURST = "burst"
COMMANDS = (OFF, ON, DROPOUT, BURST)

DEFAULT_BURST_OVERRIDES: dict[str, float] = {
    "pm25": 120.0, "pm10": 180.0, "co2": 2200.0, "tvoc": 900.0,
}
"""시나리오에 alert_burst가 없을 때 `ctl burst`가 쓰는 기본 목표값."""


class ControlError(RuntimeError):
    """제어 채널을 쓸 수 없을 때."""


@dataclass(frozen=True)
class Command:
    command: str
    device_id: str
    minutes: float | None = None


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def write_command(
    control_dir: str | Path,
    command: str,
    device_id: str,
    minutes: float | None = None,
) -> Path:
    if command not in COMMANDS:
        raise ControlError(f"알 수 없는 명령 '{command}' (사용 가능: {COMMANDS})")
    directory = Path(control_dir)
    directory.mkdir(parents=True, exist_ok=True)
    # 파일명에 시각을 넣어 사전순 = 발행순이 되게 한다. 같은 초에 두 명령이
    # 들어와도 uuid 조각으로 충돌하지 않는다.
    name = f"{COMMAND_PREFIX}{time.time():.6f}-{uuid.uuid4().hex[:8]}{COMMAND_SUFFIX}"
    path = directory / name
    _atomic_write(
        path, {"command": command, "device_id": device_id, "minutes": minutes}
    )
    return path


def drain_commands(control_dir: str | Path) -> list[Command]:
    """명령 파일을 발행순으로 읽고 지운다. 깨진 파일은 버리고 경고만 남긴다."""
    directory = Path(control_dir)
    if not directory.is_dir():
        return []

    commands: list[Command] = []
    for path in sorted(directory.glob(f"{COMMAND_PREFIX}*{COMMAND_SUFFIX}")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            command = str(raw["command"])
            device_id = str(raw["device_id"])
            minutes = raw.get("minutes")
            if command not in COMMANDS:
                raise ValueError(f"알 수 없는 명령 '{command}'")
            commands.append(
                Command(
                    command=command,
                    device_id=device_id,
                    minutes=float(minutes) if minutes is not None else None,
                )
            )
        except Exception as exc:
            # 깨진 명령 하나가 제어 채널 전체를 막으면 안 된다.
            LOG.warning("제어 명령 무시 (%s): %s", path.name, exc)
        finally:
            try:
                path.unlink()
            except OSError:
                pass
    return commands


def write_state(control_dir: str | Path, state: dict[str, Any]) -> Path:
    directory = Path(control_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / STATE_FILE
    _atomic_write(path, state)
    return path


def read_state(control_dir: str | Path) -> dict[str, Any]:
    path = Path(control_dir) / STATE_FILE
    if not path.is_file():
        raise ControlError(
            f"상태 파일이 없습니다: {path}\n"
            "러너가 실행 중인지, 같은 CONTROL_DIR을 보고 있는지 확인하세요."
        )
    try:
        # utf-8-sig: BOM이 붙은 파일도 읽는다. 사람이 손으로 열어보거나 다른
        # 도구로 만든 상태 파일에서 BOM 하나 때문에 죽지 않게 한다.
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        raise ControlError(
            f"상태 파일을 읽을 수 없습니다 ({path}): {exc}\n"
            "러너를 다시 시작하면 새로 기록됩니다."
        ) from exc
