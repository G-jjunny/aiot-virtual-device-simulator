"""메인 루프 — 디바이스 탐색, 프로비저닝, 틱 발행, 정상 종료.

captured_at은 오프셋 없이(naive) 현재 KST 벽시계 숫자로 보낸다. 백엔드가
오프셋을 파싱한 뒤 버리고 그 숫자를 그대로 timestamptz(UTC)에 저장하므로,
오프셋을 붙이면 적재 시각이 9시간 밀린다. 실제 디바이스가 남기는 데이터와
같은 KST 축에 놓이게 하려면 naive로 보내야 한다.
"""

from __future__ import annotations

import logging
import random
import signal
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from livesim.api import AdminApi, DeviceRecord
from livesim.config import DROPOUT, SILENCE, Scenario, Settings
from livesim.device import LiveDevice, MqttPublisher, Publisher
from livesim.scheduler import EventScheduler

LOG = logging.getLogger("livesim.runner")

INITIAL_BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 300.0
SLEEP_STEP_SECONDS = 0.5

_KST = timezone(timedelta(hours=9))


class RunnerError(RuntimeError):
    """실행을 시작할 수 없을 때."""


def kst_now() -> datetime:
    """naive KST 벽시계.

    오프셋 없이 발행하면 이 숫자가 그대로 UTC 컬럼에 저장되어, 실제 디바이스
    (KST 오프셋 전송 → 백엔드가 오프셋을 버림)와 동일한 captured_at이 된다.
    """
    return datetime.now(_KST).replace(tzinfo=None, microsecond=0)


@dataclass
class TickStats:
    published: int = 0
    buffered: int = 0
    silenced: int = 0
    unavailable: int = 0
    failed: int = 0
    flushed: int = 0


@dataclass
class DeviceSession:
    """디바이스 1대의 커넥션 수명 관리.

    LiveDevice는 재접속을 넘어 살아남는다 — 오프라인 버퍼가 커넥션에 딸려
    사라지면 dropout 재전송이 통째로 없어지기 때문이다. 새 커넥션은
    publisher만 교체한다.
    """

    record: DeviceRecord
    device: LiveDevice | None = None
    connected: bool = False
    next_attempt: float = 0.0
    backoff: float = 0.0


class Runner:
    def __init__(
        self,
        scenario: Scenario,
        api: AdminApi,
        connect: Callable[[DeviceRecord], Publisher],
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.scenario = scenario
        self.api = api
        self.connect = connect
        self.clock = clock
        self.scheduler = EventScheduler(
            scenario.events, scenario.interval_seconds, rng=rng
        )
        self.sessions: dict[str, DeviceSession] = {}
        self.order: list[str] = []
        self.tick_index = 0

    # ---- 준비 -----------------------------------------------------------

    def start(self) -> list[DeviceRecord]:
        self.api.login()
        records = self.api.resolve_devices(
            self.scenario.exclude_devices, self.scenario.max_devices
        )
        if not records:
            raise RunnerError(
                "발행 대상 디바이스가 없습니다. 백엔드에 디바이스가 등록되어 있는지, "
                "시나리오의 exclude_devices가 과도하지 않은지 확인하세요."
            )
        self.sessions = {record.device_id: DeviceSession(record) for record in records}
        self.order = [record.device_id for record in records]
        return records

    # ---- 실행 -----------------------------------------------------------

    def run(self, stop: Callable[[], bool] | None = None) -> None:
        stop = stop if stop is not None else _install_signal_stop()
        records = self.start()
        LOG.info(
            "%d대 발행 시작 — 시나리오 '%s', %d초 주기",
            len(records),
            self.scenario.name,
            self.scenario.interval_seconds,
        )

        next_tick = self.clock()
        try:
            while not stop():
                stats = self.tick(kst_now(), self.clock())
                LOG.info(
                    "tick %d: 발행 %d, 버퍼 %d, 재전송 %d, 침묵 %d, 미접속 %d, 실패 %d",
                    self.tick_index - 1,
                    stats.published,
                    stats.buffered,
                    stats.flushed,
                    stats.silenced,
                    stats.unavailable,
                    stats.failed,
                )
                next_tick += self.scenario.interval_seconds
                now = self.clock()
                if next_tick <= now:
                    # 발행이 주기보다 오래 걸리면 밀린 틱을 몰아 실행하게 된다.
                    # 따라잡기 폭주 대신 현재 시각 기준으로 다시 맞춘다.
                    next_tick = now + self.scenario.interval_seconds
                _sleep_until(next_tick, stop)
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
            if event is not None and event.type == SILENCE:
                # 침묵은 버퍼링도 하지 않는다 — 데이터 유실 자체를 모의한다.
                stats.silenced += 1
                continue

            offline = session.device is not None and not session.device.online
            if not offline:
                if not self._ensure_connected(session, now):
                    stats.unavailable += 1
                    continue

            device = session.device
            if device is None:
                stats.unavailable += 1
                continue
            if not offline and event is not None and event.type == DROPOUT:
                # 단절 적용은 접속을 확보한 뒤에 한다. 아직 붙어본 적 없는
                # 디바이스는 버퍼를 담을 LiveDevice 자체가 없기 때문이다.
                device.go_offline()
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
            if session.connected and session.device is not None:
                try:
                    session.device.publisher.disconnect()
                except Exception as exc:
                    LOG.warning("연결 해제 실패 (%s): %s", session.record.device_id, exc)
            session.connected = False
        if pending:
            LOG.info("종료 — 재전송하지 못한 버퍼 %d건 폐기", pending)
        LOG.info("종료 완료.")

    # ---- 커넥션 ---------------------------------------------------------

    def _ensure_connected(self, session: DeviceSession, now: float) -> bool:
        if session.connected:
            return True
        if now < session.next_attempt:
            return False

        device_id = session.record.device_id
        try:
            publisher = self.connect(session.record)
        except Exception as exc:
            session.backoff = min(
                MAX_BACKOFF_SECONDS,
                max(INITIAL_BACKOFF_SECONDS, session.backoff * 2),
            )
            session.next_attempt = now + session.backoff
            LOG.warning(
                "접속 실패 (%s): %s — %.0f초 후 재시도", device_id, exc, session.backoff
            )
            return False

        if session.device is None:
            session.device = LiveDevice(session.record, publisher)
        else:
            session.device.publisher = publisher
        device = session.device
        session.connected = True
        session.backoff = 0.0
        session.next_attempt = 0.0
        LOG.info("접속 완료: %s", device_id)

        # 단절 중이 아닌데 버퍼가 남아 있다면 이전 재전송이 실패한 것이다.
        if device.online and device.pending:
            self._resume(session)
        return True

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
            LOG.warning("버퍼 재전송 실패 (%s): %s", session.record.device_id, exc)
            self._drop(session, self.clock())
            return 0
        if flushed:
            LOG.info("버퍼 재전송 %d건 (%s)", flushed, session.record.device_id)
        if device.dropped:
            LOG.warning(
                "버퍼 상한 초과로 %d건 폐기 (%s)", device.dropped, session.record.device_id
            )
            device.dropped = 0
        return flushed

    def _drop(self, session: DeviceSession, now: float) -> None:
        if session.device is not None and session.connected:
            try:
                session.device.publisher.disconnect()
            except Exception:
                pass
        session.connected = False
        session.backoff = max(INITIAL_BACKOFF_SECONDS, session.backoff)
        session.next_attempt = now + session.backoff


def make_connector(
    settings: Settings, api: AdminApi
) -> Callable[[DeviceRecord], Publisher]:
    """디바이스별 MQTT 커넥션 팩토리.

    매 접속마다 토큰을 새로 받는다 — 재접속 시점에 옛 JWT가 만료됐을 수 있고,
    한 번 거부되면 그 디바이스는 영영 붙지 못한다.
    """

    def connect(record: DeviceRecord) -> Publisher:
        token = api.provision_device_token(record.device_id)
        publisher = MqttPublisher(
            settings.mqtt_host,
            settings.mqtt_port,
            client_id=f"livesim-{record.device_id}",
            username=record.device_id,
            password=token,
        )
        publisher.connect()
        return publisher

    return connect


def run(
    settings: Settings, scenario: Scenario, stop: Callable[[], bool] | None = None
) -> None:
    api = AdminApi(settings.api_base_url, settings.admin_username, settings.admin_password)
    Runner(scenario, api, make_connector(settings, api)).run(stop)


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


def _sleep_until(deadline: float, stop: Callable[[], bool]) -> None:
    """stop()을 주기적으로 확인하며 deadline까지 대기한다."""
    while not stop():
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        time.sleep(min(SLEEP_STEP_SECONDS, remaining))
