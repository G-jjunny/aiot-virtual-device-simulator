"""디바이스 1대의 발행 동작. MQTT 접속은 publisher 인터페이스 뒤에 둔다."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import paho.mqtt.client as mqtt

from livesim.config import DeviceCredential
from livesim.payload import NO_OFFSET, apply_overrides, build_payload, build_topic

LOG = logging.getLogger("livesim.device")

MAX_BUFFER = 288
"""오프라인 버퍼 상한 (5분 주기 기준 24시간).

무제한으로 쌓으면 장시간 단절 후 재전송에서 브로커 최대 패킷 크기를 넘겨
배치 전체가 버려진다. 상한을 넘으면 가장 오래된 측정값부터 버린다.
"""


class Publisher(Protocol):
    def publish(self, topic: str, payload_str: str, qos: int = 1) -> None: ...

    def disconnect(self) -> None: ...


class MqttPublisher:
    """paho-mqtt 기반 실제 발행기.

    username/password를 주면 connect() 이전에 username_pw_set으로 설정한다.
    EMQX가 CONNECT의 username(=device_id)과 password(=device JWT)의 sub
    클레임을 대조해 ACL을 그 device_id로 스코프하므로, 반드시 connect() 전에
    설정되어야 한다.
    """

    def __init__(
        self,
        host: str,
        port: int,
        client_id: str,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if username is not None:
            self.client.username_pw_set(username, password)

    def connect(self) -> None:
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.loop_start()

    def publish(self, topic: str, payload_str: str, qos: int = 1) -> None:
        info = self.client.publish(topic, payload_str, qos=qos)
        info.wait_for_publish(timeout=5)

    def disconnect(self) -> None:
        # DISCONNECT 패킷이 실제로 나가려면 네트워크 루프가 아직 살아 있어야
        # 한다. loop_stop을 먼저 부르면 패킷 전달 전에 스레드가 멈춘다.
        self.client.disconnect()
        self.client.loop_stop()


@dataclass
class LiveDevice:
    """측정값을 만들어 자기 토픽으로 발행하는 디바이스 1대."""

    credential: DeviceCredential
    publisher: Publisher
    online: bool = True
    captured_at_offset: str = NO_OFFSET
    max_buffer: int = MAX_BUFFER
    dropped: int = 0
    _buffer: list[dict[str, Any]] = field(default_factory=list, repr=False)

    @property
    def device_id(self) -> str:
        return self.credential.device_id

    @property
    def pending(self) -> int:
        return len(self._buffer)

    # ---- 상태 전환 -------------------------------------------------------

    def go_offline(self) -> None:
        """네트워크 단절 모의. 이후 측정값은 로컬 버퍼에 쌓인다."""
        self.online = False

    def go_online(self) -> int:
        """재연결 후 버퍼를 batch 토픽으로 한 번에 재전송하고 건수를 돌려준다."""
        self.online = True
        if not self._buffer:
            return 0

        buffered = self._buffer
        self._buffer = []
        try:
            self.publisher.publish(
                self.topic("sensor/batch"), json.dumps({"readings": buffered}), 1
            )
        except Exception:
            # 재전송이 실패했는데 버퍼를 비우면 그 구간이 영구 유실된다.
            # 되돌려 놓고 다음 재접속 때 다시 시도하게 한다.
            self._buffer = buffered + self._buffer
            raise
        return len(buffered)

    # ---- 발행 -----------------------------------------------------------

    def publish(
        self,
        ts: datetime,
        seed: int = 0,
        overrides: dict[str, float] | None = None,
    ) -> bool:
        """측정값 1건을 발행한다. 오프라인이면 버퍼에 넣고 False를 돌려준다."""
        payload = apply_overrides(self._build(ts, seed), overrides)
        if not self.online:
            self._buffer_reading(payload)
            return False
        self.publisher.publish(self.topic("sensor"), json.dumps(payload), 1)
        return True

    def _buffer_reading(self, payload: dict[str, Any]) -> None:
        self._buffer.append(payload)
        while len(self._buffer) > self.max_buffer:
            self._buffer.pop(0)
            self.dropped += 1

    # ---- 보조 -----------------------------------------------------------

    def _build(self, ts: datetime, seed: int) -> dict[str, Any]:
        return build_payload(
            self.credential.device_id,
            self.credential.site_id,
            self.credential.device_type,
            ts,
            facility_type=self.credential.facility_type,
            seed=seed,
            captured_at_offset=self.captured_at_offset,
        )

    def topic(self, suffix: str) -> str:
        return build_topic(
            self.credential.facility_type,
            self.credential.site_id,
            self.credential.device_type,
            self.credential.device_id,
            suffix,
        )
