"""백엔드 REST 클라이언트 — 디바이스 자격증명을 MQTT용 JWT로 교환하는 것 하나뿐.

EMQX는 익명 접속을 허용하지 않는다. 디바이스별 MQTT 커넥션은 그 디바이스만의
JWT(password)로 인증해야 하며, 실제 디바이스는 주입받은 device_id/secret으로
이 토큰을 직접 받아온다 (관리자 자격증명은 알지 못한다).

0.2.0에서 admin 로그인·디바이스 목록 조회·시크릿 발급을 모두 걷어냈다. 그 작업들은
관리자가 FE 대시보드에서 수행하는 일이지 디바이스가 하는 일이 아니다.
"""

from __future__ import annotations

import logging

import requests

LOG = logging.getLogger("livesim.api")

TIMEOUT = 10


class ApiError(RuntimeError):
    """API 호출이 실패했을 때. status는 HTTP 상태(네트워크 오류면 None)."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    @property
    def is_rejected(self) -> bool:
        """자격증명이 거부된 것인지 (일시적 장애가 아니라).

        4xx는 secret이 틀렸거나 폐기된 것이므로 재시도해도 달라지지 않는다.
        5xx·네트워크 오류는 백엔드 쪽 일시 장애일 수 있어 재시도 대상이다.
        """
        return self.status is not None and 400 <= self.status < 500


def safe_error(res: requests.Response) -> str:
    """응답에서 진단에 필요한 부분만 뽑는다. 제출값은 절대 포함하지 않는다.

    422의 fieldErrors[].rejectedValue에는 제출한 secret이 그대로 담겨 온다.
    본문을 통째로 로그에 남기면 평문 자격증명이 남는다.
    """
    try:
        body = res.json()
    except ValueError:
        return "(응답 본문 파싱 불가)"
    if not isinstance(body, dict):
        return "(응답 본문이 객체가 아님)"
    parts = [
        f"{key}={body[key]}"
        for key in ("errorCode", "message", "path")
        if body.get(key)
    ]
    fields = body.get("fieldErrors")
    if isinstance(fields, list) and fields:
        named = [
            f"{item.get('field')}({item.get('message')})"
            for item in fields
            if isinstance(item, dict)
        ]
        parts.append("fieldErrors=" + ", ".join(named))
    return "; ".join(parts) or "(진단 정보 없음)"


def exchange_device_token(
    api_base_url: str,
    device_id: str,
    secret: str,
    session: requests.Session | None = None,
) -> str:
    """secret을 디바이스 JWT로 교환한다.

    요청은 snake_case, 응답 키도 access_token이다. 인증 헤더 없이 호출한다 —
    device_id/secret 자체가 자격증명이다.
    """
    caller = session or requests
    url = f"{api_base_url.rstrip('/')}/auth/device/token"
    try:
        res = caller.post(
            url, json={"device_id": device_id, "secret": secret}, timeout=TIMEOUT
        )
    except requests.RequestException as exc:
        # 백엔드가 아직 안 떴거나 주소가 틀린 것은 흔한 운영 상황이다.
        # 트레이스백 대신 원인 한 줄을 보여주고, 재시도 대상으로 분류되게 한다.
        raise ApiError(f"백엔드 통신 실패 ({url}): {exc}") from exc

    if res.status_code != 200:
        raise ApiError(
            f"디바이스 토큰 교환 실패 ({device_id}, {res.status_code}): "
            f"{safe_error(res)}",
            status=res.status_code,
        )
    return res.json()["access_token"]
