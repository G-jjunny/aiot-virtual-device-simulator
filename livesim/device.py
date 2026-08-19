"""디바이스 1대의 발행 동작. MQTT 접속은 publisher 인터페이스 뒤에 둔다."""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import paho.mqtt.client as mqtt

from livesim.config import DeviceCredential
from livesim.payload import NO_OFFSET, apply_overrides, build_payload, build_topic
from livesim.profiles import DEFAULT_PROFILE

LOG = logging.getLogger("livesim.device")

MAX_BUFFER = 288
"""오프라인 버퍼 상한 (5분 주기 기준 24시간).

무제한으로 쌓으면 장시간 단절 후 재전송에서 브로커 최대 패킷 크기를 넘겨
배치 전체가 버려진다. 상한을 넘으면 가장 오래된 측정값부터 버린다.
"""


CONNACK_TIMEOUT = 10.0
PUBLISH_TIMEOUT = 5.0


class MqttError(RuntimeError):
    """브로커가 접속·발행을 받아주지 않았을 때."""


class Publisher(Protocol):
    def publish(self, topic: str, payload_str: str, qos: int = 1) -> None: ...

    def disconnect(self) -> None: ...


class MqttPublisher:
    """paho-mqtt 기반 실제 발행기.

    username/password를 주면 connect() 이전에 username_pw_set으로 설정한다.
    EMQX가 CONNECT의 username(=device_id)과 password(=device JWT)의 sub
    클레임을 대조해 ACL을 그 device_id로 스코프하므로, 반드시 connect() 전에
    설정되어야 한다.

    접속·발행 모두 브로커의 응답을 확인한 뒤에야 성공으로 친다. paho는 둘 다
    조용히 실패할 수 있어서(CONNECT는 CONNACK를 기다리지 않고 돌아오고,
    wait_for_publish는 타임아웃에도 예외를 던지지 않는다), 확인을 생략하면
    폐기된 기기가 "접속 완료 · 발행 N건"으로 보인다. 실물이라면 "보냈는데
    서버에 없다"가 되는 허위 양성이다.
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
        self.client_id = client_id
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
        if username is not None:
            self.client.username_pw_set(username, password)
        self._connack: threading.Event = threading.Event()
        self._reason: object = None
        self.client.on_connect = self._on_connect

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        self._reason = reason_code
        self._connack.set()

    def connect(self) -> None:
        """CONNACK까지 확인한다. 거부·무응답이면 정리하고 예외를 던진다."""
        self._connack.clear()
        self._reason = None
        self.client.connect(self.host, self.port, keepalive=30)
        self.client.loop_start()

        if not self._connack.wait(timeout=CONNACK_TIMEOUT):
            self._abort()
            raise MqttError(
                f"MQTT CONNACK 무응답 ({self.client_id}, {CONNACK_TIMEOUT:.0f}초)"
            )
        code = int(getattr(self._reason, "value", self._reason) or 0)
        if code != 0:
            self._abort()
            raise MqttError(
                f"MQTT 접속 거부 (banned or auth failure): rc={code} "
                f"({self._reason}) — {self.client_id}"
            )

    def _abort(self) -> None:
        """실패한 커넥션을 확실히 정리한다.

        loop_stop을 하지 않으면 paho의 네트워크 스레드가 남아 옛 자격증명으로
        무한 재접속을 시도한다. 재프로비저닝은 러너가 담당하므로, 여기서는
        죽은 커넥션을 완전히 끊어 두어야 한다.
        """
        try:
            self.client.disconnect()
        except Exception:
            pass
        try:
            self.client.loop_stop()
        except Exception:
            pass

    def publish(self, topic: str, payload_str: str, qos: int = 1) -> None:
        info = self.client.publish(topic, payload_str, qos=qos)
        info.wait_for_publish(timeout=PUBLISH_TIMEOUT)
        if not info.is_published():
            # wait_for_publish는 타임아웃에도 조용히 돌아온다. 이 확인이 없으면
            # 브로커가 전부 차단해도 발행 건수만 늘어난다.
            raise MqttError(
                f"MQTT 발행 미확인 ({topic}, rc={info.rc}, "
                f"{PUBLISH_TIMEOUT:.0f}초 내 PUBACK 없음)"
            )

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
    profile: str = DEFAULT_PROFILE
    """이 기기가 놓인 환경 등급. 3계층 우선순위 해석 결과를 러너가 넣어준다."""
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
            preset=self.profile,
        )

    def topic(self, suffix: str) -> str:
        return build_topic(
            self.credential.facility_type,
            self.credential.site_id,
            self.credential.device_type,
            self.credential.device_id,
            suffix,
        )
