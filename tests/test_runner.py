import json
import random
from datetime import datetime

import pytest

from livesim import control
from livesim.api import ApiError
from livesim.config import DeviceCredential, EventSpec, Scenario
from livesim.runner import Runner, RunnerError, kst_now, select_devices

TS = datetime(2026, 7, 20, 14, 30, 0)
INTERVAL = 300
CERTAIN = 288.0  # 86400/300 → 틱당 확률 1.0


def credential(device_id: str) -> DeviceCredential:
    return DeviceCredential(
        device_id=device_id,
        secret=f"secret-{device_id}",
        site_id="S-1",
        device_type="FIXED",
        facility_type="OFFICE",
    )


def scenario(events=(), interval: int = INTERVAL, exclude=(), max_devices=0) -> Scenario:
    return Scenario(
        name="test",
        description="",
        interval_seconds=interval,
        max_devices=max_devices,
        exclude_devices=tuple(exclude),
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

    def __init__(self, connect_failures: int = 0, publish_failures: int = 0, error=None):
        self.connect_failures = connect_failures
        self.publish_failures = publish_failures
        self.error = error or ConnectionError("no broker")
        self.calls: list[str] = []
        self.publishers: list[FakePublisher] = []

    def __call__(self, cred: DeviceCredential) -> FakePublisher:
        self.calls.append(cred.device_id)
        if self.connect_failures > 0:
            self.connect_failures -= 1
            raise self.error
        fails = self.publish_failures > 0
        if fails:
            self.publish_failures -= 1
        publisher = FakePublisher(fail=fails)
        self.publishers.append(publisher)
        return publisher


def make_runner(device_ids, events=(), connector=None, seed: int = 1, **kwargs):
    connector = connector or FakeConnector()
    runner = Runner(
        kwargs.pop("scenario", None) or scenario(events),
        [credential(item) for item in device_ids],
        connector,
        rng=random.Random(seed),
        clock=lambda: 0.0,
        **kwargs,
    )
    return runner, connector


# ---- 인벤토리 핫 리로드 -------------------------------------------------


def entry(device_id: str, secret: str | None = None) -> str:
    return (
        f"  - device_id: {device_id}\n"
        f"    secret: {secret or f'secret-{device_id}'}\n"
        f"    site_id: S-1\n    device_type: FIXED\n    facility_type: OFFICE\n"
    )


def reload_runner(tmp_path, device_ids=("AQ-1",), **kwargs):
    path = tmp_path / "devices.yaml"
    path.write_text("devices:\n" + "".join(entry(d) for d in device_ids), encoding="utf-8")
    runner, connector = make_runner(list(device_ids), devices_file=str(path), **kwargs)
    runner.start()
    return runner, connector, path


def test_reload_adds_new_devices(tmp_path):
    runner, _, path = reload_runner(tmp_path)

    path.write_text("devices:\n" + entry("AQ-1") + entry("AQ-2"), encoding="utf-8")
    result = runner.reload_inventory()

    assert result.ok is True
    assert result.added == ("AQ-2",)
    assert runner.order == ["AQ-1", "AQ-2"]


def test_newly_added_device_publishes_next_tick(tmp_path):
    runner, connector, path = reload_runner(tmp_path)
    runner.tick(TS, now=0.0)

    path.write_text("devices:\n" + entry("AQ-1") + entry("AQ-2"), encoding="utf-8")
    runner.reload_inventory()
    stats = runner.tick(TS, now=300.0)

    assert stats.published == 2
    assert "AQ-2" in connector.calls


def test_reload_removes_and_disconnects_dropped_devices(tmp_path):
    runner, connector, path = reload_runner(tmp_path, ("AQ-1", "AQ-2"))
    runner.tick(TS, now=0.0)

    path.write_text("devices:\n" + entry("AQ-1"), encoding="utf-8")
    result = runner.reload_inventory()

    assert result.removed == ("AQ-2",)
    assert runner.order == ["AQ-1"]
    assert "AQ-2" not in runner.sessions
    assert connector.publishers[1].disconnected is True


def test_reload_swaps_a_changed_secret_without_dropping_the_connection(tmp_path):
    """지금 붙어 있는 커넥션은 유효하다 — 다음 재접속 때 새 secret을 쓴다."""
    runner, _, path = reload_runner(tmp_path)
    runner.tick(TS, now=0.0)

    path.write_text("devices:\n" + entry("AQ-1", "rotated"), encoding="utf-8")
    result = runner.reload_inventory()

    assert result.rotated == ("AQ-1",)
    assert runner.sessions["AQ-1"].credential.secret == "rotated"
    assert runner.sessions["AQ-1"].device.credential.secret == "rotated"
    assert runner.sessions["AQ-1"].connected is True  # 끊지 않았다


def test_rotated_secret_re_enables_a_rejected_device(tmp_path):
    """시크릿을 고쳐 넣었으면 재시작 없이 다시 시도할 기회를 줘야 한다."""
    connector = FakeConnector(connect_failures=1, error=ApiError("거부", status=401))
    runner, _, path = reload_runner(tmp_path, connector=connector)
    runner.tick(TS, now=0.0)
    assert runner.sessions["AQ-1"].disabled is True

    path.write_text("devices:\n" + entry("AQ-1", "rotated"), encoding="utf-8")
    runner.reload_inventory()

    assert runner.sessions["AQ-1"].disabled is False
    assert runner.tick(TS, now=10.0).published == 1


def test_reload_keeps_fleet_when_file_is_broken(tmp_path):
    """운영 중 오타 하나로 돌던 플릿이 죽으면 리로드 실패보다 훨씬 나쁘다."""
    runner, _, path = reload_runner(tmp_path, ("AQ-1", "AQ-2"))

    path.write_text("devices:\n  - device_id: 'unclosed\n", encoding="utf-8")
    result = runner.reload_inventory()

    assert result.ok is False
    assert "YAML" in result.error
    assert runner.order == ["AQ-1", "AQ-2"]


def test_reload_keeps_fleet_when_file_is_missing(tmp_path):
    runner, _, path = reload_runner(tmp_path)

    path.unlink()
    result = runner.reload_inventory()

    assert result.ok is False
    assert runner.order == ["AQ-1"]


def test_reload_respects_scenario_exclude_and_max(tmp_path):
    path = tmp_path / "devices.yaml"
    path.write_text("devices:\n" + entry("AQ-1"), encoding="utf-8")
    runner, _ = make_runner(
        ["AQ-1"], devices_file=str(path), scenario=scenario(exclude=["AQ-2"])
    )
    runner.start()

    path.write_text(
        "devices:\n" + entry("AQ-1") + entry("AQ-2") + entry("AQ-3"), encoding="utf-8"
    )
    runner.reload_inventory()

    assert runner.order == ["AQ-1", "AQ-3"]  # AQ-2는 시나리오가 제외


def test_reload_command_is_routed(tmp_path):
    control_dir = tmp_path / "control"
    runner, _, path = reload_runner(tmp_path, control_dir=str(control_dir))

    path.write_text("devices:\n" + entry("AQ-1") + entry("AQ-2"), encoding="utf-8")
    control.write_command(control_dir, control.RELOAD)
    runner.drain_control()

    assert runner.order == ["AQ-1", "AQ-2"]


def test_reload_without_devices_file_is_a_noop_failure():
    runner, _ = make_runner(["AQ-1"])
    runner.start()

    result = runner.reload_inventory()

    assert result.ok is False
    assert runner.order == ["AQ-1"]


# ---- 인벤토리 기반 선별 -------------------------------------------------


def test_start_uses_inventory_without_any_api_call():
    runner, _ = make_runner(["AQ-2", "AQ-1"])

    runner.start()

    assert runner.order == ["AQ-1", "AQ-2"]  # device_id 정렬로 결정적


def test_exclude_devices_temporarily_disables_entries():
    inventory = [credential("AQ-1"), credential("AQ-2")]

    selected = select_devices(inventory, scenario(exclude=["AQ-2"]))

    assert [item.device_id for item in selected] == ["AQ-1"]


def test_max_devices_caps_the_inventory():
    inventory = [credential(f"AQ-{n}") for n in range(5)]

    selected = select_devices(inventory, scenario(max_devices=2))

    assert len(selected) == 2


def test_start_without_devices_is_an_error():
    runner, _ = make_runner([])

    with pytest.raises(RunnerError, match="devices.yaml"):
        runner.start()


# ---- 정상 발행 ---------------------------------------------------------


def test_tick_connects_and_publishes_for_every_device():
    runner, connector = make_runner(["AQ-1", "AQ-2"])
    runner.start()

    stats = runner.tick(TS, now=0.0)

    assert stats.published == 2
    assert connector.calls == ["AQ-1", "AQ-2"]
    topic, body, _ = connector.publishers[0].published[0]
    assert topic == "aiot/v1/office/S-1/fixed/AQ-1/sensor"
    assert json.loads(body)["captured_at"] == "2026-07-20T14:30:00"


def test_second_tick_reuses_the_existing_connection():
    runner, connector = make_runner(["AQ-1"])
    runner.start()

    runner.tick(TS, now=0.0)
    runner.tick(TS, now=300.0)

    assert connector.calls == ["AQ-1"]
    assert len(connector.publishers[0].published) == 2


# ---- 자격증명 거부 ------------------------------------------------------


def test_rejected_secret_disables_only_that_device():
    """4xx는 재시도해도 같다 — 그 디바이스만 접고 나머지는 계속 발행한다."""
    connector = FakeConnector(connect_failures=1, error=ApiError("거부", status=401))
    runner, _ = make_runner(["AQ-1", "AQ-2"], connector=connector)
    runner.start()

    first = runner.tick(TS, now=0.0)

    assert first.disabled == 1
    assert first.published == 1
    assert runner.sessions["AQ-1"].disabled is True
    assert "401" in runner.sessions["AQ-1"].disabled_reason


def test_disabled_device_is_not_retried():
    connector = FakeConnector(connect_failures=1, error=ApiError("거부", status=403))
    runner, _ = make_runner(["AQ-1"], connector=connector)
    runner.start()

    runner.tick(TS, now=0.0)
    runner.tick(TS, now=600.0)

    assert connector.calls == ["AQ-1"]  # 두 번째 틱에서 재시도하지 않음


def test_server_error_is_retried_with_backoff():
    """5xx는 백엔드 일시 장애일 수 있으므로 비활성화하면 안 된다."""
    connector = FakeConnector(connect_failures=1, error=ApiError("장애", status=503))
    runner, _ = make_runner(["AQ-1"], connector=connector)
    runner.start()

    first = runner.tick(TS, now=0.0)
    later = runner.tick(TS, now=10.0)

    assert first.unavailable == 1
    assert runner.sessions["AQ-1"].disabled is False
    assert later.published == 1


# ---- 확률 이벤트 -------------------------------------------------------


def test_dropout_buffers_then_resends_as_batch_on_recovery():
    events = [EventSpec("dropout", CERTAIN, (5.0, 5.0))]
    runner, connector = make_runner(["AQ-1"], events)
    runner.start()

    first = runner.tick(TS, now=0.0)
    second = runner.tick(TS.replace(minute=35), now=299.0)
    third = runner.tick(TS.replace(minute=40), now=300.0)

    assert first.buffered == 1
    assert second.buffered == 1
    assert third.flushed == 2

    batches = [
        item for item in connector.publishers[0].published
        if item[0].endswith("/sensor/batch")
    ]
    assert len(batches) == 1
    readings = json.loads(batches[0][1])["readings"]
    assert [r["captured_at"] for r in readings] == [
        "2026-07-20T14:30:00",
        "2026-07-20T14:35:00",
    ]


def test_silence_publishes_nothing_and_buffers_nothing():
    events = [EventSpec("silence", CERTAIN, (5.0, 5.0))]
    runner, connector = make_runner(["AQ-1"], events)
    runner.start()

    stats = runner.tick(TS, now=0.0)

    assert stats.silenced == 1
    assert stats.published == 0
    assert connector.publishers == []


def test_alert_burst_pushes_values_toward_the_target():
    events = [EventSpec("alert_burst", CERTAIN, (30.0, 30.0), {"pm25": 120.0})]
    runner, connector = make_runner(["AQ-1"], events)
    runner.start()

    runner.tick(TS, now=0.0)

    payload = json.loads(connector.publishers[0].published[0][1])
    assert 108.0 <= payload["pm25"] <= 132.0  # 목표값 ±10%


# ---- 수동 제어 ---------------------------------------------------------


def test_ctl_off_stops_publishing_and_disconnects(tmp_path):
    runner, connector = make_runner(["AQ-1"], control_dir=str(tmp_path))
    runner.start()
    runner.tick(TS, now=0.0)

    control.write_command(tmp_path, control.OFF, "AQ-1")
    runner.drain_control()
    stats = runner.tick(TS, now=300.0)

    assert stats.powered_off == 1
    assert stats.published == 0
    assert stats.buffered == 0  # 꺼진 기기는 측정 자체를 하지 않는다
    assert connector.publishers[0].disconnected is True


def test_ctl_on_resumes_publishing(tmp_path):
    runner, connector = make_runner(["AQ-1"], control_dir=str(tmp_path))
    runner.start()
    runner.tick(TS, now=0.0)

    control.write_command(tmp_path, control.OFF, "AQ-1")
    runner.drain_control()
    runner.tick(TS, now=300.0)

    control.write_command(tmp_path, control.ON, "AQ-1")
    runner.drain_control()
    stats = runner.tick(TS, now=600.0)

    assert stats.published == 1
    assert len(connector.calls) == 2  # 새 커넥션으로 재접속


def test_ctl_on_after_dropout_resumes_publishing(tmp_path):
    """release()는 plan.ended를 거치지 않아 _resume이 자동으로 불리지 않는다.

    online을 되돌리지 않으면 그 디바이스는 영영 버퍼링만 하며 재접속조차 하지 않는다.
    """
    runner, connector = make_runner(["AQ-1"], control_dir=str(tmp_path))
    runner.start()
    runner.tick(TS, now=0.0)

    control.write_command(tmp_path, control.DROPOUT, "AQ-1", minutes=60)
    runner.drain_control()
    buffering = runner.tick(TS.replace(minute=35), now=10.0)

    control.write_command(tmp_path, control.ON, "AQ-1")
    runner.drain_control()
    resumed = runner.tick(TS.replace(minute=40), now=20.0)

    assert buffering.buffered == 1
    assert resumed.published == 1
    assert runner.sessions["AQ-1"].device.pending == 0  # 버퍼도 비워져야 한다


def test_ctl_on_after_off_during_dropout_recovers(tmp_path):
    """dropout → off → on 순서에서도 갇히지 않아야 한다."""
    runner, _ = make_runner(["AQ-1"], control_dir=str(tmp_path))
    runner.start()
    runner.tick(TS, now=0.0)

    control.write_command(tmp_path, control.DROPOUT, "AQ-1", minutes=60)
    runner.drain_control()
    runner.tick(TS, now=10.0)

    control.write_command(tmp_path, control.OFF, "AQ-1")
    runner.drain_control()
    runner.tick(TS, now=20.0)

    control.write_command(tmp_path, control.ON, "AQ-1")
    runner.drain_control()
    stats = runner.tick(TS, now=30.0)

    assert stats.published == 1


def test_manual_off_suppresses_probability_events(tmp_path):
    """수동 off 중에는 확률 이벤트가 얹히면 안 된다."""
    events = [EventSpec("alert_burst", CERTAIN, (30.0, 30.0), {"pm25": 120.0})]
    runner, _ = make_runner(["AQ-1"], events, control_dir=str(tmp_path))
    runner.start()

    control.write_command(tmp_path, control.OFF, "AQ-1")
    runner.drain_control()
    stats = runner.tick(TS, now=0.0)

    assert stats.powered_off == 1
    assert runner.scheduler.describe("AQ-1")[0] == "power_off"


def test_ctl_dropout_buffers_then_flushes_after_duration(tmp_path):
    runner, connector = make_runner(["AQ-1"], control_dir=str(tmp_path))
    runner.start()
    runner.tick(TS, now=0.0)

    control.write_command(tmp_path, control.DROPOUT, "AQ-1", minutes=5)
    runner.drain_control()
    during = runner.tick(TS.replace(minute=35), now=100.0)
    after = runner.tick(TS.replace(minute=40), now=301.0)

    assert during.buffered == 1
    assert after.flushed == 1


def test_ctl_burst_uses_scenario_overrides(tmp_path):
    events = [EventSpec("alert_burst", 0.1, (10.0, 30.0), {"pm25": 300.0})]
    runner, connector = make_runner(["AQ-1"], events, control_dir=str(tmp_path))
    runner.start()

    control.write_command(tmp_path, control.BURST, "AQ-1", minutes=10)
    runner.drain_control()
    runner.tick(TS, now=0.0)

    payload = json.loads(connector.publishers[0].published[0][1])
    assert 270.0 <= payload["pm25"] <= 330.0


def test_ctl_burst_falls_back_to_builtin_defaults(tmp_path):
    runner, connector = make_runner(["AQ-1"], control_dir=str(tmp_path))
    runner.start()

    control.write_command(tmp_path, control.BURST, "AQ-1", minutes=10)
    runner.drain_control()
    runner.tick(TS, now=0.0)

    payload = json.loads(connector.publishers[0].published[0][1])
    expected = control.DEFAULT_BURST_OVERRIDES["pm25"]
    assert expected * 0.9 <= payload["pm25"] <= expected * 1.1


def test_command_for_unknown_device_is_ignored(tmp_path):
    runner, _ = make_runner(["AQ-1"], control_dir=str(tmp_path))
    runner.start()

    control.write_command(tmp_path, control.OFF, "GHOST-99")

    assert runner.drain_control() == 1  # 읽기는 했지만
    assert runner.scheduler.describe("GHOST-99") is None  # 상태는 만들지 않음


def test_drain_without_control_dir_is_a_noop():
    runner, _ = make_runner(["AQ-1"])

    assert runner.drain_control() == 0


# ---- 상태 스냅샷 -------------------------------------------------------


def test_snapshot_reports_device_state(tmp_path):
    runner, _ = make_runner(["AQ-1"], control_dir=str(tmp_path))
    runner.start()
    runner.tick(TS, now=0.0)

    snapshot = runner.snapshot()
    device = snapshot["devices"][0]

    assert snapshot["scenario"] == "test"
    assert device["device_id"] == "AQ-1"
    assert device["connected"] is True
    assert device["online"] is True
    assert device["pending"] == 0
    assert device["event"] is None


def test_snapshot_marks_manual_events(tmp_path):
    runner, _ = make_runner(["AQ-1"], control_dir=str(tmp_path))
    runner.start()

    control.write_command(tmp_path, control.DROPOUT, "AQ-1", minutes=5)
    runner.drain_control()
    device = runner.snapshot()["devices"][0]

    assert device["event"] == "dropout"
    assert device["event_manual"] is True
    assert device["event_ends_in"] == 300.0


def test_power_off_has_no_end_time(tmp_path):
    """사람이 켤 때까지 유지되므로 남은 시간이라는 개념이 없다."""
    runner, _ = make_runner(["AQ-1"], control_dir=str(tmp_path))
    runner.start()

    control.write_command(tmp_path, control.OFF, "AQ-1")
    runner.drain_control()

    assert runner.snapshot()["devices"][0]["event_ends_in"] is None


def test_state_file_is_written_after_commands(tmp_path):
    runner, _ = make_runner(["AQ-1"], control_dir=str(tmp_path))
    runner.start()

    control.write_command(tmp_path, control.OFF, "AQ-1")
    runner.drain_control()

    assert control.read_state(tmp_path)["devices"][0]["event"] == "power_off"


def test_snapshot_never_contains_secrets(tmp_path):
    runner, _ = make_runner(["AQ-1"], control_dir=str(tmp_path))
    runner.start()

    assert "secret-AQ-1" not in json.dumps(runner.snapshot(), ensure_ascii=False)


# ---- 내구성 ------------------------------------------------------------


def test_connect_failure_does_not_stop_other_devices():
    connector = FakeConnector(connect_failures=1)
    runner, _ = make_runner(["AQ-1", "AQ-2"], connector=connector)
    runner.start()

    stats = runner.tick(TS, now=0.0)

    assert stats.unavailable == 1
    assert stats.published == 1


def test_failed_device_waits_for_backoff_then_retries():
    connector = FakeConnector(connect_failures=1)
    runner, _ = make_runner(["AQ-1"], connector=connector)
    runner.start()

    runner.tick(TS, now=0.0)
    during_backoff = runner.tick(TS, now=1.0)
    after_backoff = runner.tick(TS, now=10.0)

    assert during_backoff.unavailable == 1
    assert connector.calls == ["AQ-1", "AQ-1"]
    assert after_backoff.published == 1


def test_publish_failure_drops_the_connection_and_reprovisions():
    """paho 자동 재접속은 만료된 옛 JWT를 재사용하므로 커넥션을 새로 만든다."""
    connector = FakeConnector(publish_failures=1)
    runner, _ = make_runner(["AQ-1"], connector=connector)
    runner.start()

    failed = runner.tick(TS, now=0.0)
    recovered = runner.tick(TS, now=10.0)

    assert failed.failed == 1
    assert connector.publishers[0].disconnected is True
    assert len(connector.calls) == 2
    assert recovered.published == 1


def test_pending_buffer_survives_reconnect_and_is_flushed():
    """재전송이 실패한 구간이 커넥션과 함께 사라지면 안 된다."""
    runner, connector = make_runner(["AQ-1"])
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
        item for item in connector.publishers[1].published
        if item[0].endswith("/sensor/batch")
    ]
    assert len(batches) == 1


def test_shutdown_disconnects_every_connected_device():
    runner, connector = make_runner(["AQ-1", "AQ-2"])
    runner.start()
    runner.tick(TS, now=0.0)

    runner.shutdown()

    assert all(publisher.disconnected for publisher in connector.publishers)


def test_run_stops_immediately_when_asked():
    runner, connector = make_runner(["AQ-1"])

    runner.run(stop=lambda: True)

    assert connector.calls == []


def test_run_loop_drives_repeated_ticks_then_shuts_down():
    """tick()을 직접 부르는 테스트만으로는 루프 본문이 한 번도 실행되지 않는다."""
    runner, connector = make_runner(["AQ-1", "AQ-2"])

    runner.run(stop=lambda: runner.tick_index >= 3)

    assert runner.tick_index == 3
    assert [len(p.published) for p in connector.publishers] == [3, 3]
    assert all(p.disconnected for p in connector.publishers)


def test_run_loop_keeps_going_after_a_device_fails():
    connector = FakeConnector(publish_failures=1)
    runner, _ = make_runner(["AQ-1"], connector=connector)

    runner.run(stop=lambda: runner.tick_index >= 3)

    assert runner.tick_index == 3


# ---- 시각 --------------------------------------------------------------


def test_kst_now_is_naive_and_second_resolution():
    now = kst_now()
    assert now.tzinfo is None
    assert now.microsecond == 0
