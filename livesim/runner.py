"""메인 루프 — 인벤토리 로딩, 토큰 교환, 틱 발행, 제어 명령 처리, 정상 종료.

captured_at은 오프셋 없이(naive) 현재 KST 벽시계 숫자로 보낸다. 백엔드가
오프셋을 파싱한 뒤 버리고 그 숫자를 그대로 timestamptz(UTC)에 저장하므로,
오프셋을 붙이면 적재 시각이 9시간 밀린다. 실제 디바이스가 남기는 데이터와
같은 KST 축에 놓이게 하려면 naive로 보내야 한다.

0.2.0에서 admin API 디스커버리를 걷어냈다. 발행 대상은 devices.yaml에 주입된
자격증명이 전부이며, 프로비저닝은 secret→JWT 교환 한 단계뿐이다.
"""

from __future__ import annotations

import logging
import random
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

from livesim import control
from livesim.api import ApiError, exchange_device_token
from livesim.config import (
    ALERT_BURST,
    DROPOUT,
    POWER_OFF,
    SILENCE,
    ConfigError,
    DeviceCredential,
    Scenario,
    Settings,
    load_inventory,
)
from livesim.device import LiveDevice, MqttPublisher, Publisher
from livesim.scheduler import EventScheduler

LOG = logging.getLogger("livesim.runner")

INITIAL_BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 300.0
CONTROL_POLL_SECONDS = 1.0

_KST = timezone(timedelta(hours=9))


class RunnerError(RuntimeError):
    """실행을 시작할 수 없을 때."""


def kst_now() -> datetime:
    """naive KST 벽시계.

    오프셋 없이 발행하면 이 숫자가 그대로 UTC 컬럼에 저장되어, 실제 디바이스
    (KST 오프셋 전송 → 백엔드가 오프셋을 버림)와 동일한 captured_at이 된다.
    """
    return datetime.now(_KST).replace(tzinfo=None, microsecond=0)


def select_devices(
    inventory: Sequence[DeviceCredential], scenario: Scenario
) -> list[DeviceCredential]:
    """시나리오의 일시 비활성 목록과 상한을 적용해 발행 대상을 고른다."""
    excluded = set(scenario.exclude_devices)
    selected = [item for item in inventory if item.device_id not in excluded]
    selected.sort(key=lambda item: item.device_id)
    if scenario.max_devices > 0:
        selected = selected[: scenario.max_devices]
    return selected


@dataclass
class TickStats:
    published: int = 0
    buffered: int = 0
    silenced: int = 0
    unavailable: int = 0
    failed: int = 0
    flushed: int = 0
    powered_off: int = 0
    disabled: int = 0


@dataclass(frozen=True)
class ReloadResult:
    ok: bool
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    rotated: tuple[str, ...] = ()
    error: str = ""


@dataclass
class DeviceSession:
    """디바이스 1대의 커넥션 수명 관리.

    LiveDevice는 재접속을 넘어 살아남는다 — 오프라인 버퍼가 커넥션에 딸려
    사라지면 dropout 재전송이 통째로 없어지기 때문이다. 새 커넥션은
    publisher만 교체한다.
    """

    credential: DeviceCredential
    device: LiveDevice | None = None
    connected: bool = False
    next_attempt: float = 0.0
    backoff: float = 0.0
    disabled: bool = False
    disabled_reason: str = ""


class Runner:
    def __init__(
        self,
        scenario: Scenario,
        inventory: Sequence[DeviceCredential],
        connect: Callable[[DeviceCredential], Publisher],
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.time,
        control_dir: str | None = None,
        devices_file: str | None = None,
    ) -> None:
        self.scenario = scenario
        self.inventory = tuple(inventory)
        self.connect = connect
        self.clock = clock
        self.control_dir = control_dir
        self.devices_file = devices_file
        self.scheduler = EventScheduler(
            scenario.events, scenario.interval_seconds, rng=rng
        )
        self.sessions: dict[str, DeviceSession] = {}
        self.order: list[str] = []
        self.tick_index = 0

    # ---- 준비 -----------------------------------------------------------

    def start(self) -> list[DeviceCredential]:
        selected = select_devices(self.inventory, self.scenario)
        if not selected:
            raise RunnerError(
                "발행 대상 디바이스가 없습니다. devices.yaml에 항목이 있는지, "
                "시나리오의 exclude_devices가 과도하지 않은지 확인하세요."
            )
        self.sessions = {
            item.device_id: DeviceSession(item) for item in selected
        }
        self.order = [item.device_id for item in selected]
        return selected

    # ---- 실행 -----------------------------------------------------------

    def run(self, stop: Callable[[], bool] | None = None) -> None:
        stop = stop if stop is not None else _install_signal_stop()
        selected = self.start()
        LOG.info(
            "%d대 발행 시작 — 시나리오 '%s', %d초 주기",
            len(selected),
            self.scenario.name,
            self.scenario.interval_seconds,
        )
        if self.control_dir:
            LOG.info("제어 채널: %s (livesim ctl ...)", self.control_dir)

        next_tick = self.clock()
        try:
            while not stop():
                stats = self.tick(kst_now(), self.clock())
                LOG.info(
                    "tick %d: 발행 %d, 버퍼 %d, 재전송 %d, 침묵 %d, 전원off %d, "
                    "미접속 %d, 실패 %d, 비활성 %d",
                    self.tick_index - 1,
                    stats.published,
                    stats.buffered,
                    stats.flushed,
                    stats.silenced,
                    stats.powered_off,
                    stats.unavailable,
                    stats.failed,
                    stats.disabled,
                )
                self._publish_state()
                next_tick += self.scenario.interval_seconds
                now = self.clock()
                if next_tick <= now:
                    # 발행이 주기보다 오래 걸리면 밀린 틱을 몰아 실행하게 된다.
                    # 따라잡기 폭주 대신 현재 시각 기준으로 다시 맞춘다.
                    next_tick = now + self.scenario.interval_seconds
                _sleep_until(next_tick, stop, on_poll=self.drain_control)
        finally:
            self.shutdown()

    def tick(self, ts: datetime, now: float) -> TickStats:
        stats = TickStats()
        plan = self.scheduler.tick(self.order, now)

        for event in plan.ended:
            LOG.info("이벤트 종료: %s %s", event.type, event.device_id)
            if event.type == DROPOUT:
                stats.flushed += self._resume(self.sessions[event.device_id])
        for event in plan.started:
            LOG.info(
                "이벤트 시작: %s %s (%.1f분)",
                event.type,
                event.device_id,
                (event.ends_at - now) / 60.0,
            )

        for index, device_id in enumerate(self.order):
            session = self.sessions[device_id]
            event = plan.active.get(device_id)

            if session.disabled:
                stats.disabled += 1
                continue
            if event is not None and event.type == POWER_OFF:
                # 전원 차단은 발행도 버퍼링도 하지 않는다. 꺼진 기기는
                # 측정 자체를 하지 않으므로 나중에 재전송할 것도 없다.
                stats.powered_off += 1
                continue
            if event is not None and event.type == SILENCE:
                # 침묵은 버퍼링도 하지 않는다 — 데이터 유실 자체를 모의한다.
                stats.silenced += 1
                continue

            offline = session.device is not None and not session.device.online
            if not offline:
                if not self._ensure_connected(session, now):
                    stats.unavailable += int(not session.disabled)
                    stats.disabled += int(session.disabled)
                    continue
                if event is not None and event.type == DROPOUT:
                    # 단절 적용은 접속을 확보한 뒤에 한다. 아직 붙어본 적 없는
                    # 디바이스는 버퍼를 담을 LiveDevice 자체가 없기 때문이다.
                    session.device.go_offline()

            device = session.device
            if device is None:
                stats.unavailable += 1
                continue
            try:
                overrides = event.overrides if event is not None else None
                if device.publish(ts, seed=self.tick_index * 1000 + index,
                                  overrides=overrides):
                    stats.published += 1
                else:
                    stats.buffered += 1
            except Exception as exc:
                # 발행 예외는 대개 소켓/인증이 죽었다는 뜻이다. paho의 자동
                # 재접속은 옛 JWT를 그대로 재사용하므로, 커넥션을 버리고
                # 다음 시도에서 토큰부터 새로 받는다.
                LOG.warning("발행 실패 (%s): %s", device_id, exc)
                stats.failed += 1
                self._drop(session, now)

        self.tick_index += 1
        return stats

    def shutdown(self) -> None:
        """끊기 실패가 다른 디바이스 정리를 막지 않게 한다."""
        pending = 0
        for session in self.sessions.values():
            if session.device is not None:
                pending += session.device.pending
            self._disconnect(session)
        if pending:
            LOG.info("종료 — 재전송하지 못한 버퍼 %d건 폐기", pending)
        LOG.info("종료 완료.")

    # ---- 제어 채널 -------------------------------------------------------

    def drain_control(self) -> int:
        """제어 디렉터리의 명령을 읽어 적용한다. 적용한 개수를 돌려준다."""
        if not self.control_dir:
            return 0
        commands = control.drain_commands(self.control_dir)
        for command in commands:
            self._apply_command(command)
        if commands:
            # 명령은 1초 폴링으로 즉시 반영되는데 상태 기록이 다음 틱이면,
            # 버튼을 눌러도 최대 한 틱(기본 5분) 동안 화면이 그대로다.
            # 반대로 명령이 없을 때까지 쓰면 1초마다 디스크를 두드리게 된다.
            self._publish_state()
        return len(commands)

    def _apply_command(self, command: control.Command) -> None:
        if command.command == control.RELOAD:
            # 플릿 전체 대상이라 device_id가 없다.
            self.reload_inventory()
            return

        session = self.sessions.get(command.device_id)
        if session is None:
            LOG.warning(
                "제어 명령 대상 없음: %s (발행 대상이 아닙니다)", command.device_id
            )
            return

        now = self.clock()
        seconds = command.minutes * 60 if command.minutes is not None else None

        if command.command == control.OFF:
            self.scheduler.force(command.device_id, POWER_OFF, now)
            self._disconnect(session)
            # 사람이 직접 끈 것이므로 백오프를 남기지 않는다. on 하면 즉시 붙어야 한다.
            session.next_attempt = 0.0
            session.backoff = 0.0
            LOG.info("[ctl] 전원 off: %s", command.device_id)
        elif command.command == control.ON:
            self.scheduler.release(command.device_id)
            session.next_attempt = 0.0
            session.backoff = 0.0
            if session.device is not None and not session.device.online:
                # release()는 plan.ended를 거치지 않으므로 dropout 만료 때와 달리
                # _resume이 자동으로 불리지 않는다. 되돌려주지 않으면 이 디바이스는
                # 영영 버퍼링만 하며 재접속조차 시도하지 않는다. 만료 경로와 같은
                # 복구 로직을 그대로 쓴다 (접속돼 있으면 버퍼도 즉시 재전송).
                self._resume(session)
            LOG.info("[ctl] 전원 on: %s (다음 틱에 재접속)", command.device_id)
        elif command.command == control.DROPOUT:
            self.scheduler.force(command.device_id, DROPOUT, now, seconds)
            if session.device is not None:
                session.device.go_offline()
            LOG.info("[ctl] 통신 단절: %s (%s분)", command.device_id, command.minutes)
        elif command.command == control.BURST:
            self.scheduler.force(
                command.device_id, ALERT_BURST, now, seconds, self._burst_overrides()
            )
            LOG.info("[ctl] 오염 급증: %s (%s분)", command.device_id, command.minutes)

    def reload_inventory(self) -> ReloadResult:
        """devices.yaml을 다시 읽어 세션을 맞춘다 (추가/제거/시크릿 변경).

        파일이 깨져 있으면 기존 인벤토리를 그대로 유지한다. 운영 중 오타 하나로
        돌고 있는 플릿 전체가 죽는 것이 리로드 실패보다 훨씬 나쁘다.
        """
        if not self.devices_file:
            return ReloadResult(ok=False, error="인벤토리 경로가 설정되지 않았습니다")
        try:
            inventory = load_inventory(self.devices_file)
        except ConfigError as exc:
            LOG.warning("인벤토리 리로드 거부 — 기존 %d대 유지: %s", len(self.order), exc)
            return ReloadResult(ok=False, error=str(exc))

        self.inventory = tuple(inventory)
        selected = select_devices(inventory, self.scenario)
        wanted = {item.device_id: item for item in selected}
        current = set(self.sessions)

        added = [device_id for device_id in wanted if device_id not in current]
        removed = [device_id for device_id in current if device_id not in wanted]
        rotated: list[str] = []

        for device_id in removed:
            session = self.sessions.pop(device_id)
            self._disconnect(session)
            self.scheduler.release(device_id)

        for device_id, credential in wanted.items():
            session = self.sessions.get(device_id)
            if session is None:
                self.sessions[device_id] = DeviceSession(credential)
                continue
            if session.credential == credential:
                continue
            # 시크릿·소속이 바뀌었다. 지금 붙어 있는 커넥션은 유효하므로 끊지 않고,
            # 다음 재접속 때 새 값으로 토큰을 받게 자격증명만 바꿔 끼운다.
            session.credential = credential
            if session.device is not None:
                session.device.credential = credential
            if session.disabled:
                # 거부됐던 디바이스는 새 시크릿으로 다시 시도할 기회를 준다.
                session.disabled = False
                session.disabled_reason = ""
                session.next_attempt = 0.0
                session.backoff = 0.0
            rotated.append(device_id)

        self.order = sorted(self.sessions)
        LOG.info(
            "인벤토리 리로드: 추가 %d, 제거 %d, 자격증명 변경 %d (총 %d대)",
            len(added), len(removed), len(rotated), len(self.order),
        )
        return ReloadResult(
            ok=True, added=tuple(added), removed=tuple(removed), rotated=tuple(rotated)
        )

    def _burst_overrides(self) -> dict[str, float]:
        """시나리오의 alert_burst 목표값을 재사용하고, 없으면 내장 기본값."""
        for spec in self.scenario.events:
            if spec.type == ALERT_BURST and spec.overrides:
                return dict(spec.overrides)
        return dict(control.DEFAULT_BURST_OVERRIDES)

    def snapshot(self) -> dict:
        """디바이스별 현재 상태. state.json으로 기록되고 `ctl status`가 읽는다."""
        now = self.clock()
        devices = []
        for device_id in self.order:
            session = self.sessions[device_id]
            described = self.scheduler.describe(device_id)
            event_type, ends_at, manual = described or (None, None, False)
            finite = ends_at is not None and ends_at != float("inf")
            devices.append({
                "device_id": device_id,
                "connected": session.connected,
                "online": session.device.online if session.device else False,
                "pending": session.device.pending if session.device else 0,
                "event": event_type,
                "event_manual": manual,
                # 기록 순간의 잔여 시간. 이 파일은 틱마다(기본 5분) 갱신되므로
                # 그동안 값이 얼어붙는다 — 하위 호환용으로만 남긴다.
                "event_ends_in": round(max(0.0, ends_at - now), 1) if finite else None,
                # 절대 종료시각. 읽는 쪽이 '지금' 기준으로 다시 계산할 수 있어야
                # 갱신 주기와 무관하게 카운트다운이 흐른다.
                "event_ends_at": float(ends_at) if finite else None,
                "disabled": session.disabled,
                "disabled_reason": session.disabled_reason,
            })
        return {
            "updated_at": kst_now().isoformat(),
            # 기록 시점의 epoch. 클라이언트가 자기 시계와의 차이를 보정하는 기준.
            "written_at": now,
            "tick": self.tick_index,
            "scenario": self.scenario.name,
            "interval_seconds": self.scenario.interval_seconds,
            "devices": devices,
        }

    def _publish_state(self) -> None:
        if not self.control_dir:
            return
        try:
            control.write_state(self.control_dir, self.snapshot())
        except OSError as exc:
            # 상태 기록 실패가 발행을 막으면 안 된다 (읽기 전용 마운트 등).
            LOG.warning("상태 파일 기록 실패: %s", exc)

    # ---- 커넥션 ---------------------------------------------------------

    def _ensure_connected(self, session: DeviceSession, now: float) -> bool:
        if session.connected:
            return True
        if now < session.next_attempt:
            return False

        device_id = session.credential.device_id
        try:
            publisher = self.connect(session.credential)
        except ApiError as exc:
            if exc.is_rejected:
                # 4xx는 secret이 폐기·오기입된 것이라 재시도해도 같다. 이 디바이스만
                # 접고 나머지는 계속 발행한다 (devices.yaml 수정 후 재기동 필요).
                session.disabled = True
                session.disabled_reason = f"자격증명 거부 ({exc.status})"
                LOG.error(
                    "자격증명 거부 — %s 발행 제외: %s. devices.yaml의 secret을 "
                    "확인하고 재기동하세요.",
                    device_id,
                    exc,
                )
                return False
            return self._schedule_retry(session, now, exc)
        except Exception as exc:
            return self._schedule_retry(session, now, exc)

        if session.device is None:
            session.device = LiveDevice(session.credential, publisher)
        else:
            session.device.publisher = publisher
        session.connected = True
        session.backoff = 0.0
        session.next_attempt = 0.0
        LOG.info("접속 완료: %s", device_id)

        # 단절 중이 아닌데 버퍼가 남아 있다면 이전 재전송이 실패한 것이다.
        if session.device.online and session.device.pending:
            self._resume(session)
        return True

    def _schedule_retry(
        self, session: DeviceSession, now: float, exc: Exception
    ) -> bool:
        session.backoff = min(
            MAX_BACKOFF_SECONDS, max(INITIAL_BACKOFF_SECONDS, session.backoff * 2)
        )
        session.next_attempt = now + session.backoff
        LOG.warning(
            "접속 실패 (%s): %s — %.0f초 후 재시도",
            session.credential.device_id,
            exc,
            session.backoff,
        )
        return False

    def _resume(self, session: DeviceSession) -> int:
        """dropout 종료 — 버퍼를 batch로 재전송한다."""
        device = session.device
        if device is None:
            return 0
        if not session.connected:
            # 아직 붙지 못했으면 online 표시만 하고 버퍼는 남긴다.
            device.online = True
            return 0
        try:
            flushed = device.go_online()
        except Exception as exc:
            LOG.warning("버퍼 재전송 실패 (%s): %s", session.credential.device_id, exc)
            self._drop(session, self.clock())
            return 0
        if flushed:
            LOG.info("버퍼 재전송 %d건 (%s)", flushed, session.credential.device_id)
        if device.dropped:
            LOG.warning(
                "버퍼 상한 초과로 %d건 폐기 (%s)",
                device.dropped,
                session.credential.device_id,
            )
            device.dropped = 0
        return flushed

    def _disconnect(self, session: DeviceSession) -> None:
        if session.device is not None and session.connected:
            try:
                session.device.publisher.disconnect()
            except Exception as exc:
                LOG.warning(
                    "연결 해제 실패 (%s): %s", session.credential.device_id, exc
                )
        session.connected = False

    def _drop(self, session: DeviceSession, now: float) -> None:
        self._disconnect(session)
        session.backoff = max(INITIAL_BACKOFF_SECONDS, session.backoff)
        session.next_attempt = now + session.backoff


def make_connector(settings: Settings) -> Callable[[DeviceCredential], Publisher]:
    """디바이스별 MQTT 커넥션 팩토리.

    매 접속마다 토큰을 새로 받는다 — 재접속 시점에 옛 JWT가 만료됐을 수 있고,
    paho의 자동 재접속은 저장된 옛 password를 그대로 재사용하기 때문이다.
    """

    def connect(credential: DeviceCredential) -> Publisher:
        token = exchange_device_token(
            settings.api_base_url, credential.device_id, credential.secret
        )
        publisher = MqttPublisher(
            settings.mqtt_host,
            settings.mqtt_port,
            client_id=f"livesim-{credential.device_id}",
            username=credential.device_id,
            password=token,
        )
        publisher.connect()
        return publisher

    return connect


def run(
    settings: Settings,
    scenario: Scenario,
    inventory: Sequence[DeviceCredential],
    stop: Callable[[], bool] | None = None,
) -> None:
    Runner(
        scenario,
        inventory,
        make_connector(settings),
        control_dir=settings.control_dir,
        devices_file=settings.devices_file,
    ).run(stop)


def _install_signal_stop() -> Callable[[], bool]:
    """SIGINT/SIGTERM을 정상 종료 신호로 받는다 (docker stop은 SIGTERM)."""
    stopped = {"flag": False}

    def handler(signum: int, frame: object) -> None:
        stopped["flag"] = True
        LOG.info("종료 신호 수신 (%s) — 연결 해제 중...", signum)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, AttributeError):
            # 메인 스레드가 아니거나 플랫폼이 지원하지 않으면 건너뛴다.
            pass
    return lambda: stopped["flag"]


def _sleep_until(
    deadline: float,
    stop: Callable[[], bool],
    on_poll: Callable[[], object] | None = None,
) -> None:
    """stop()을 확인하며 deadline까지 대기하고, 1초마다 제어 명령을 처리한다."""
    while not stop():
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(CONTROL_POLL_SECONDS, remaining))
        if on_poll is not None:
            on_poll()
