import json
from datetime import datetime
from unittest.mock import patch

import pytest

from livesim.config import DeviceCredential
from livesim.device import LiveDevice, MqttPublisher

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


def test_mqtt_publisher_sets_credentials_before_connect():
    """EMQX가 CONNECT의 username/password(device JWT)를 검증하므로,
    username_pw_set은 client.connect()보다 먼저 호출되어야 한다."""
    with patch("livesim.device.mqtt.Client") as MockClient:
        mock_client = MockClient.return_value
        publisher = MqttPublisher(
            "host", 1883, "cid", username="AQ-CT-001", password="device-jwt"
        )
        publisher.connect()

        mock_client.username_pw_set.assert_called_once_with("AQ-CT-001", "device-jwt")
        call_names = [call[0] for call in mock_client.mock_calls]
        assert call_names.index("username_pw_set") < call_names.index("connect")


def test_mqtt_publisher_without_credentials_skips_username_pw_set():
    with patch("livesim.device.mqtt.Client") as MockClient:
        mock_client = MockClient.return_value
        MqttPublisher("host", 1883, "cid").connect()
        mock_client.username_pw_set.assert_not_called()
