"""보안 리허설 — 디바이스 측에서 확인할 수 있는 거부 동작 3종.

여기서 확인하는 것은 "우리가 들어갈 수 있다"가 아니라 **"들어갈 수 없어야 하는데
정말 막히는가"**다. 세 케이스 모두 백엔드/브로커가 거부해야 PASS이고, 통과되면
그건 심각한 인증 결함이다.

디바이스가 가진 정보만으로 수행한다 — 관리자 권한도, DB 접근도 필요 없다.
"""

from __future__ import annotations

import logging
import secrets
import uuid
from dataclasses import dataclass
from typing import Callable, Sequence

from livesim.api import ApiError, exchange_device_token
from livesim.config import DeviceCredential, Settings

LOG = logging.getLogger("livesim.rehearse")

MQTT_PROBE_TIMEOUT = 10.0

# 교환기가 토큰을 내주면 안 되는 자격증명들. 실제 secret은 절대 쓰지 않는다.
UNKNOWN_DEVICE_PREFIX = "LIVESIM-NOPE-"


def mqtt_subject(inventory: Sequence[DeviceCredential]) -> DeviceCredential | None:
    """SEC-02·03이 대상으로 삼을 기기. **MQTT 경로의 기기여야 한다.**

    ation_http 기기는 secret도 MQTT 신원도 없어서, 그걸 고르면 SEC-02가 "등록된
    기기의 secret 검증"이 아니라 사실상 SEC-01(미등록)을 한 번 더 하는 것이 된다.
    거부되니 PASS는 뜨지만 검증한 것이 없다 — 리허설이 조용히 아무것도 안 하게 된다.
    """
    for item in inventory:
        if not item.uses_ation_http:
            return item
    return None


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    description: str
    passed: bool
    detail: str


TokenExchange = Callable[[str, str, str], str]
"""(api_base_url, device_id, secret) -> token. 실패는 ApiError."""

ACCEPTED = "accepted"
REJECTED = "rejected"
UNREACHABLE = "unreachable"

MqttProbe = Callable[[str, int, str, str], str]
"""(host, port, username, password) -> ACCEPTED | REJECTED | UNREACHABLE.

브로커가 꺼져 있는 것과 브로커가 우리를 걷어찬 것을 반드시 구분해야 한다.
둘을 뭉뚱그리면 인프라가 없을 때 리허설이 통과해버려, 아무것도 검증하지 않은
결과가 "정상"으로 보고된다.
"""


def _probe_mqtt(host: str, port: int, username: str, password: str) -> str:
    """위조 자격증명으로 CONNECT를 시도하고 결과를 세 갈래로 판정한다.

    paho는 CONNACK을 네트워크 루프에서 처리하므로 콜백으로 받아야 한다.
    CONNACK 없이 연결을 끊는 브로커도 있어서, TCP가 붙은 뒤 끊긴 것은
    거부로 본다 (TCP 자체가 안 붙은 것과는 구분된다).
    """
    import threading

    import paho.mqtt.client as mqtt

    settled = threading.Event()
    outcome: dict[str, str | None] = {"result": None}

    def on_connect(client, userdata, flags, reason_code, properties=None):
        code = int(getattr(reason_code, "value", reason_code))
        outcome["result"] = ACCEPTED if code == 0 else REJECTED
        settled.set()

    def on_disconnect(client, userdata, *args):
        if outcome["result"] is None:
            # CONNACK을 주지 않고 끊는 브로커 — TCP는 붙었으니 거부로 본다.
            outcome["result"] = REJECTED
        settled.set()

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"livesim-rehearse-{uuid.uuid4().hex[:8]}",
    )
    client.username_pw_set(username, password)
    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    try:
        client.connect(host, port, keepalive=10)
    except Exception as exc:
        # 브로커가 없다 — "막혔다"는 증거가 아니다.
        LOG.debug("MQTT 프로브 연결 실패: %s", exc)
        return UNREACHABLE
    try:
        client.loop_start()
        settled.wait(timeout=MQTT_PROBE_TIMEOUT)
    finally:
        try:
            client.disconnect()
            client.loop_stop()
        except Exception:
            pass
    # 응답이 없으면 판단하지 않는다.
    return outcome["result"] or UNREACHABLE


def _case_unknown_device(
    settings: Settings, exchange: TokenExchange
) -> CaseResult:
    device_id = f"{UNKNOWN_DEVICE_PREFIX}{uuid.uuid4().hex[:12]}"
    try:
        exchange(settings.api_base_url, device_id, secrets.token_urlsafe(24))
    except ApiError as exc:
        if exc.is_rejected:
            return CaseResult(
                "SEC-01", "미등록 device_id로 토큰 교환", True,
                f"거부됨 (HTTP {exc.status}) — 정상",
            )
        return CaseResult(
            "SEC-01", "미등록 device_id로 토큰 교환", False,
            f"거부 여부를 확인할 수 없음: {exc}",
        )
    return CaseResult(
        "SEC-01", "미등록 device_id로 토큰 교환", False,
        "토큰이 발급됨 — 등록되지 않은 단말이 MQTT에 접속할 수 있습니다",
    )


def _case_wrong_secret(
    settings: Settings, inventory: Sequence[DeviceCredential], exchange: TokenExchange
) -> CaseResult:
    if not inventory:
        return CaseResult(
            "SEC-02", "등록된 device_id + 틀린 secret", False,
            "인벤토리가 비어 있어 검사할 수 없습니다",
        )
    subject = mqtt_subject(inventory)
    if subject is None:
        return CaseResult(
            "SEC-02", "등록된 device_id + 틀린 secret", False,
            "MQTT 경로의 기기가 인벤토리에 없어 검사할 수 없습니다 "
            "(ation_http 기기는 secret을 쓰지 않습니다)",
        )
    device_id = subject.device_id
    try:
        exchange(settings.api_base_url, device_id, f"wrong-{secrets.token_urlsafe(16)}")
    except ApiError as exc:
        if exc.is_rejected:
            return CaseResult(
                "SEC-02", "등록된 device_id + 틀린 secret", True,
                f"거부됨 (HTTP {exc.status}) — 정상",
            )
        return CaseResult(
            "SEC-02", "등록된 device_id + 틀린 secret", False,
            f"거부 여부를 확인할 수 없음: {exc}",
        )
    return CaseResult(
        "SEC-02", "등록된 device_id + 틀린 secret", False,
        f"토큰이 발급됨 — {device_id}의 secret 검증이 동작하지 않습니다",
    )


def _case_forged_jwt(
    settings: Settings, inventory: Sequence[DeviceCredential], probe: MqttProbe
) -> CaseResult:
    subject = mqtt_subject(inventory)
    device_id = subject.device_id if subject is not None else "LIVESIM-PROBE"
    result = probe(
        settings.mqtt_host, settings.mqtt_port, device_id, secrets.token_urlsafe(32)
    )
    if result == ACCEPTED:
        return CaseResult(
            "SEC-03", "위조 JWT로 MQTT CONNECT", False,
            "브로커가 접속을 수락함 — 임의 문자열로 발행이 가능합니다",
        )
    if result == REJECTED:
        return CaseResult(
            "SEC-03", "위조 JWT로 MQTT CONNECT", True, "거부됨 — 정상"
        )
    return CaseResult(
        "SEC-03", "위조 JWT로 MQTT CONNECT", False,
        f"브로커({settings.mqtt_host}:{settings.mqtt_port})에 닿지 못해 거부 여부를 "
        "확인할 수 없음 — 브로커가 떠 있는지 확인하세요",
    )


def rehearse(
    settings: Settings,
    inventory: Sequence[DeviceCredential],
    exchange: TokenExchange | None = None,
    probe: MqttProbe | None = None,
) -> list[CaseResult]:
    """3개 케이스를 순서대로 수행하고 결과를 돌려준다."""
    exchange = exchange or (
        lambda base, device_id, secret: exchange_device_token(base, device_id, secret)
    )
    probe = probe or _probe_mqtt
    return [
        _case_unknown_device(settings, exchange),
        _case_wrong_secret(settings, inventory, exchange),
        _case_forged_jwt(settings, inventory, probe),
    ]
