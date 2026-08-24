import json
from datetime import datetime

import pytest

from livesim.config import DeviceCredential
from livesim.device import LiveDevice, MqttError, MqttPublisher

TS = datetime(2026, 7, 20, 14, 30, 0)

RECORD = DeviceCredential(
    device_id="AQ-CT-001",
    secret="s3cr3t",
    site_id="S-1",
    device_type="FIXED",
    facility_type="OFFICE",
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


def make_device(**kwargs) -> LiveDevice:
    return LiveDevice(RECORD, kwargs.pop("publisher", FakePublisher()), **kwargs)


def test_publishes_to_single_sensor_topic():
    device = make_device()
    assert device.publish(TS) is True

    topic, body, qos = device.publisher.published[0]
    assert topic == "aiot/v1/office/S-1/fixed/AQ-CT-001/sensor"
    assert qos == 1
    assert json.loads(body)["device_id"] == "AQ-CT-001"


def test_publishes_naive_kst_captured_at_by_default():
    """livesim의 기본 발행 형식 — 오프셋을 붙이면 적재 시각이 9시간 밀린다."""
    device = make_device()
    device.publish(TS)
    assert json.loads(device.publisher.published[0][1])["captured_at"] == (
        "2026-07-20T14:30:00"
    )


def test_wire_payload_uses_the_facility_corrected_waveform():
    """발행 경로 끝까지 시설이 전달되는지 — 여기서 끊기면 등급 보정이 무의미하다."""
    daycare = LiveDevice(
        DeviceCredential(
            device_id="AQ-DC-001",
            secret="s",
            site_id="S-1",
            device_type="FIXED",
            facility_type="DAYCARE",
        ),
        FakePublisher(),
    )
    office = make_device()
    daycare.publish(TS)
    office.publish(TS)

    # 어린이집 tvoc '나쁨' 경계는 106 — 사무실은 밴드가 없어 일반값 그대로다.
    assert json.loads(daycare.publisher.published[0][1])["tvoc"] <= 85
    assert json.loads(office.publisher.published[0][1])["tvoc"] > 85


def test_overrides_are_applied_to_wire_payload():
    device = make_device()
    device.publish(TS, overrides={"pm25": 150})
    assert json.loads(device.publisher.published[0][1])["pm25"] == 150.0


def test_offline_publish_buffers_instead_of_sending():
    device = make_device()
    device.go_offline()

    assert device.publish(TS) is False
    assert device.publisher.published == []
    assert device.pending == 1


def test_going_online_flushes_buffer_as_single_batch():
    device = make_device()
    device.go_offline()
    device.publish(TS)
    device.publish(TS.replace(minute=35))

    flushed = device.go_online()

    assert flushed == 2
    assert len(device.publisher.published) == 1
    topic, body, _ = device.publisher.published[0]
    assert topic.endswith("/sensor/batch")
    readings = json.loads(body)["readings"]
    assert len(readings) == 2
    assert [r["captured_at"] for r in readings] == [
        "2026-07-20T14:30:00",
        "2026-07-20T14:35:00",
    ]
    assert device.pending == 0


def test_going_online_without_buffer_publishes_nothing():
    device = make_device()
    device.go_offline()
    assert device.go_online() == 0
    assert device.publisher.published == []


def test_buffer_is_restored_when_flush_fails():
    """재전송 실패로 버퍼를 비우면 그 구간이 영구 유실된다."""
    device = make_device(publisher=FakePublisher(fail=True))
    device.go_offline()
    device.publish(TS)

    with pytest.raises(ConnectionError):
        device.go_online()

    assert device.pending == 1


def test_buffer_drops_oldest_beyond_limit():
    device = make_device(max_buffer=2)
    device.go_offline()
    for minute in (30, 31, 32):
        device.publish(TS.replace(minute=minute))

    device.go_online()

    readings = json.loads(device.publisher.published[0][1])["readings"]
    assert [r["captured_at"] for r in readings] == [
        "2026-07-20T14:31:00",
        "2026-07-20T14:32:00",
    ]
    assert device.dropped == 1


def test_publish_after_reconnect_uses_new_publisher():
    """재접속은 커넥션만 교체하고 버퍼는 유지해야 한다."""
    device = make_device()
    device.go_offline()
    device.publish(TS)

    device.publisher = FakePublisher()
    flushed = device.go_online()

    assert flushed == 1
    assert len(device.publisher.published) == 1


def test_mqtt_publisher_sets_credentials_before_connect(monkeypatch):
    """EMQX가 CONNECT의 username/password(device JWT)를 검증하므로,
    username_pw_set은 client.connect()보다 먼저 호출되어야 한다."""
    publisher = make_publisher(monkeypatch)
    publisher.connect()

    calls = publisher.client.calls
    assert calls.index("username_pw_set") < calls.index("connect")


def test_mqtt_publisher_without_credentials_skips_username_pw_set(monkeypatch):
    monkeypatch.setattr("livesim.device.mqtt.Client", FakeMqttClient)
    publisher = MqttPublisher("host", 1883, "cid")

    publisher.connect()

    assert "username_pw_set" not in publisher.client.calls


# ---- CONNACK·발행 확인 --------------------------------------------------
#
# 실전에서 폐기(ban)된 기기가 "접속 완료 · 발행 25건"으로 보이고 DB에는 0건이었다.
# paho는 두 지점 모두에서 조용히 실패한다: connect()는 CONNACK를 기다리지 않고
# 돌아오고, wait_for_publish는 타임아웃에도 예외를 던지지 않는다.


class FakeInfo:
    def __init__(self, published: bool = True, rc: int = 0):
        self._published = published
        self.rc = rc
        self.waited = None

    def wait_for_publish(self, timeout=None):
        self.waited = timeout

    def is_published(self):
        return self._published


class FakeMqttClient:
    """CONNACK를 실제로 흘려주는 paho 대역."""

    reason_code = 0
    send_connack = True
    publish_ok = True

    def __init__(self, *args, **kwargs):
        self.calls: list[str] = []
        self.on_connect = None
        self.published: list[tuple] = []
        self.infos: list[FakeInfo] = []

    def username_pw_set(self, username, password=None):
        self.calls.append("username_pw_set")

    def connect(self, host, port, keepalive=None):
        self.calls.append("connect")

    def loop_start(self):
        self.calls.append("loop_start")
        # 실제 paho도 네트워크 루프가 돌기 시작해야 CONNACK를 전달한다.
        if self.send_connack and self.on_connect:
            self.on_connect(self, None, {}, self.reason_code)

    def loop_stop(self):
        self.calls.append("loop_stop")

    def disconnect(self):
        self.calls.append("disconnect")

    def publish(self, topic, payload, qos=1):
        self.published.append((topic, payload, qos))
        info = FakeInfo(self.publish_ok)
        self.infos.append(info)
        return info


def make_publisher(monkeypatch, **client_attrs) -> MqttPublisher:
    cls = type("PatchedClient", (FakeMqttClient,), client_attrs)
    monkeypatch.setattr("livesim.device.mqtt.Client", cls)
    return MqttPublisher("host", 1883, "AQ-CT-001", username="AQ-CT-001", password="jwt")


def test_connect_succeeds_when_connack_is_accepted(monkeypatch):
    publisher = make_publisher(monkeypatch)

    publisher.connect()

    assert "loop_start" in publisher.client.calls
    assert "loop_stop" not in publisher.client.calls


def test_connect_raises_when_broker_rejects(monkeypatch):
    """ban된 기기는 '접속 완료'가 아니라 접속 실패로 보여야 한다."""
    publisher = make_publisher(monkeypatch, reason_code=135)  # not authorized

    with pytest.raises(MqttError) as excinfo:
        publisher.connect()

    message = str(excinfo.value)
    assert "접속 거부" in message
    assert "rc=135" in message          # 진단에 필요한 reason code를 남긴다
    assert "AQ-CT-001" in message       # 어느 기기인지도


def test_rejected_connection_is_torn_down(monkeypatch):
    """loop_stop을 빠뜨리면 paho가 옛 자격증명으로 무한 재접속한다."""
    publisher = make_publisher(monkeypatch, reason_code=135)

    with pytest.raises(MqttError):
        publisher.connect()

    assert "loop_stop" in publisher.client.calls
    assert "disconnect" in publisher.client.calls


def test_connect_raises_on_connack_timeout(monkeypatch):
    monkeypatch.setattr("livesim.device.CONNACK_TIMEOUT", 0.05)
    publisher = make_publisher(monkeypatch, send_connack=False)

    with pytest.raises(MqttError, match="CONNACK 무응답"):
        publisher.connect()

    assert "loop_stop" in publisher.client.calls


@pytest.mark.parametrize(
    ("identifier", "accepted"),
    [(0, True), (135, False), (138, False), (128, False)],
)
def test_real_connack_reason_codes_are_classified(monkeypatch, identifier, accepted):
    """진짜 paho ReasonCode 객체로 판정을 확인한다.

    성공(0) 판정이 틀리면 정상 기기까지 전부 접속 실패한다. 거부는 실제 EMQX로
    확인했지만 성공은 유효한 자격증명이 없어 확인할 수 없으므로, 최소한 실제
    객체를 통과시켜 둔다. 135=Not authorized, 138=Banned (ban당한 기기가 받는 값).
    """
    from paho.mqtt.packettypes import PacketTypes
    from paho.mqtt.reasoncodes import ReasonCode

    reason = ReasonCode(PacketTypes.CONNACK, identifier=identifier)
    publisher = make_publisher(monkeypatch, reason_code=reason)

    if accepted:
        publisher.connect()
        assert "loop_stop" not in publisher.client.calls
    else:
        with pytest.raises(MqttError, match=f"rc={identifier}"):
            publisher.connect()


def test_publish_raises_when_broker_never_acks(monkeypatch):
    """wait_for_publish는 타임아웃에도 예외를 던지지 않는다.

    이 확인이 없으면 브로커가 전부 차단해도 발행 건수만 올라간다.
    """
    publisher = make_publisher(monkeypatch, publish_ok=False)
    publisher.connect()

    with pytest.raises(MqttError, match="발행 미확인"):
        publisher.publish("aiot/v1/x", "{}")


def test_publish_succeeds_when_acked(monkeypatch):
    publisher = make_publisher(monkeypatch)
    publisher.connect()

    publisher.publish("aiot/v1/x", "{}", qos=1)

    assert publisher.client.published == [("aiot/v1/x", "{}", 1)]
    assert publisher.client.infos[0].waited == 5.0
