import json
import random
from datetime import datetime

import pytest

from livesim.api import DeviceRecord
from livesim.config import EventSpec, Scenario
from livesim.runner import Runner, RunnerError, kst_now

TS = datetime(2026, 7, 20, 14, 30, 0)
INTERVAL = 300
CERTAIN = 288.0  # 86400/300 → 틱당 확률 1.0


def record(device_id: str) -> DeviceRecord:
    return DeviceRecord(
        device_id=device_id,
        device_type="FIXED",
        site_id="S-1",
        facility_type="OFFICE",
        status="ONLINE",
    )


def scenario(events=(), interval: int = INTERVAL) -> Scenario:
    return Scenario(
        name="test",
        description="",
        interval_seconds=interval,
        max_devices=0,
        exclude_devices=(),
        events=tuple(events),
    )


class FakePublisher:
    def __init__(self, fail: bool = False):
        self.published: list[tuple[str, str, int]] = []
        self.fail = fail
        self.disconnected = False

    def publish(self, topic, payload_str, qos=1):
        if self.fail:
            raise ConnectionError("broker gone")
        self.published.append((topic, payload_str, qos))

    def disconnect(self):
        self.disconnected = True


class FakeConnector:
    """접속 팩토리. 초기 N회는 접속 실패, 그다음 M개는 발행이 실패하는 커넥션."""

    def __init__(self, connect_failures: int = 0, publish_failures: int = 0):
        self.connect_failures = connect_failures
        self.publish_failures = publish_failures
        self.calls: list[str] = []
        self.publishers: list[FakePublisher] = []

    def __call__(self, device: DeviceRecord) -> FakePublisher:
        self.calls.append(device.device_id)
        if self.connect_failures > 0:
            self.connect_failures -= 1
            raise ConnectionError("no broker")
        fails = self.publish_failures > 0
        if fails:
            self.publish_failures -= 1
        publisher = FakePublisher(fail=fails)
        self.publishers.append(publisher)
        return publisher


class FakeApi:
    def __init__(self, records):
        self.records = records
        self.logins = 0

    def login(self):
        self.logins += 1

    def resolve_devices(self, exclude=(), max_devices=0):
        return list(self.records)


def make_runner(records, events=(), connector=None, seed: int = 1):
    connector = connector or FakeConnector()
    runner = Runner(
        scenario(events),
        FakeApi(records),
        connector,
        rng=random.Random(seed),
        clock=lambda: 0.0,
    )
    return runner, connector


# ---- 준비 --------------------------------------------------------------


def test_start_logs_in_and_builds_a_session_per_device():
    runner, _ = make_runner([record("AQ-1"), record("AQ-2")])

    runner.start()

    assert runner.api.logins == 1
    assert runner.order == ["AQ-1", "AQ-2"]


def test_start_without_devices_is_an_error():
    runner, _ = make_runner([])

    with pytest.raises(RunnerError, match="발행 대상 디바이스가 없습니다"):
        runner.start()


# ---- 정상 발행 ---------------------------------------------------------


def test_tick_connects_and_publishes_for_every_device():
    runner, connector = make_runner([record("AQ-1"), record("AQ-2")])
    runner.start()

    stats = runner.tick(TS, now=0.0)

    assert stats.published == 2
    assert connector.calls == ["AQ-1", "AQ-2"]
    topic, body, _ = connector.publishers[0].published[0]
    assert topic == "aiot/v1/office/S-1/fixed/AQ-1/sensor"
    assert json.loads(body)["captured_at"] == "2026-07-20T14:30:00"


def test_second_tick_reuses_the_existing_connection():
    runner, connector = make_runner([record("AQ-1")])
    runner.start()

    runner.tick(TS, now=0.0)
    runner.tick(TS, now=300.0)

    assert connector.calls == ["AQ-1"]
    assert len(connector.publishers[0].published) == 2


def test_devices_get_distinct_seeds_within_a_tick():
    """같은 seed를 쓰면 모든 디바이스가 완전히 같은 값을 보고한다."""
    runner, connector = make_runner([record("AQ-1"), record("AQ-2")])
    runner.start()
    runner.tick(TS, now=0.0)

    first = json.loads(connector.publishers[0].published[0][1])
    second = json.loads(connector.publishers[1].published[0][1])

    assert first["pm25"] != second["pm25"]


# ---- 이벤트 ------------------------------------------------------------


def test_dropout_buffers_then_resends_as_batch_on_recovery():
    events = [EventSpec("dropout", CERTAIN, (5.0, 5.0))]
    runner, connector = make_runner([record("AQ-1")], events)
    runner.start()

    first = runner.tick(TS, now=0.0)
    second = runner.tick(TS.replace(minute=35), now=299.0)
    third = runner.tick(TS.replace(minute=40), now=300.0)

    assert first.buffered == 1
    assert second.buffered == 1
    assert third.flushed == 2

    publisher = connector.publishers[0]
    batches = [item for item in publisher.published if item[0].endswith("/sensor/batch")]
    assert len(batches) == 1
    readings = json.loads(batches[0][1])["readings"]
    assert [r["captured_at"] for r in readings] == [
        "2026-07-20T14:30:00",
        "2026-07-20T14:35:00",
    ]


def test_silence_publishes_nothing_and_buffers_nothing():
    events = [EventSpec("silence", CERTAIN, (5.0, 5.0))]
    runner, connector = make_runner([record("AQ-1")], events)
    runner.start()

    stats = runner.tick(TS, now=0.0)

    assert stats.silenced == 1
    assert stats.published == 0
    assert stats.buffered == 0
    assert connector.publishers == []


def test_alert_burst_pushes_values_toward_the_target():
    events = [EventSpec("alert_burst", CERTAIN, (30.0, 30.0), {"pm25": 120.0})]
    runner, connector = make_runner([record("AQ-1")], events)
    runner.start()

    runner.tick(TS, now=0.0)

    payload = json.loads(connector.publishers[0].published[0][1])
    assert 108.0 <= payload["pm25"] <= 132.0  # 목표값 ±10%


# ---- 내구성 ------------------------------------------------------------


def test_connect_failure_does_not_stop_other_devices():
    connector = FakeConnector(connect_failures=1)
    runner, _ = make_runner([record("AQ-1"), record("AQ-2")], connector=connector)
    runner.start()

    stats = runner.tick(TS, now=0.0)

    assert stats.unavailable == 1
    assert stats.published == 1


def test_failed_device_waits_for_backoff_then_retries():
    connector = FakeConnector(connect_failures=1)
    runner, _ = make_runner([record("AQ-1")], connector=connector)
    runner.start()

    runner.tick(TS, now=0.0)
    during_backoff = runner.tick(TS, now=1.0)
    after_backoff = runner.tick(TS, now=10.0)

    assert during_backoff.unavailable == 1
    assert connector.calls == ["AQ-1", "AQ-1"]  # 백오프 중에는 재시도하지 않음
    assert after_backoff.published == 1


def test_publish_failure_drops_the_connection_and_reprovisions():
    """paho 자동 재접속은 만료된 옛 JWT를 재사용하므로 커넥션을 새로 만든다."""
    connector = FakeConnector(publish_failures=1)
    runner, _ = make_runner([record("AQ-1")], connector=connector)
    runner.start()

    failed = runner.tick(TS, now=0.0)
    recovered = runner.tick(TS, now=10.0)

    assert failed.failed == 1
    assert connector.publishers[0].disconnected is True
    assert len(connector.calls) == 2
    assert recovered.published == 1


def test_pending_buffer_survives_reconnect_and_is_flushed():
    """재전송이 실패한 구간이 커넥션과 함께 사라지면 안 된다."""
    runner, connector = make_runner([record("AQ-1")])
    runner.start()
    runner.tick(TS, now=0.0)

    session = runner.sessions["AQ-1"]
    session.device.go_offline()
    session.device.publish(TS.replace(minute=35))
    session.device.online = True  # 재전송에 실패해 버퍼만 남은 상태
    runner._drop(session, now=0.0)

    stats = runner.tick(TS.replace(minute=40), now=10.0)

    assert stats.published == 1
    batches = [
        item
        for item in connector.publishers[1].published
        if item[0].endswith("/sensor/batch")
    ]
    assert len(batches) == 1
    assert len(json.loads(batches[0][1])["readings"]) == 1


def test_shutdown_disconnects_every_connected_device():
    runner, connector = make_runner([record("AQ-1"), record("AQ-2")])
    runner.start()
    runner.tick(TS, now=0.0)

    runner.shutdown()

    assert all(publisher.disconnected for publisher in connector.publishers)


def test_run_stops_immediately_when_asked():
    runner, connector = make_runner([record("AQ-1")])

    runner.run(stop=lambda: True)

    assert connector.calls == []  # 첫 틱 전에 종료


def test_run_loop_drives_repeated_ticks_then_shuts_down():
    """tick()을 직접 부르는 테스트만으로는 루프 본문(마감 시각 계산, 로그, 정리)이
    한 번도 실행되지 않는다."""
    runner, connector = make_runner([record("AQ-1"), record("AQ-2")])

    runner.run(stop=lambda: runner.tick_index >= 3)

    assert runner.tick_index == 3
    assert [len(p.published) for p in connector.publishers] == [3, 3]
    assert all(p.disconnected for p in connector.publishers)


def test_run_loop_keeps_going_after_a_device_fails():
    """한 대의 발행 실패가 루프를 멈추면 24시간 운영이 성립하지 않는다."""
    connector = FakeConnector(publish_failures=1)
    runner, _ = make_runner([record("AQ-1")], connector=connector)

    runner.run(stop=lambda: runner.tick_index >= 3)

    assert runner.tick_index == 3


# ---- 시각 --------------------------------------------------------------


def test_kst_now_is_naive_and_second_resolution():
    now = kst_now()
    assert now.tzinfo is None
    assert now.microsecond == 0
