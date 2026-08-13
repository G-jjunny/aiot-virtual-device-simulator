"""aiot 백엔드 REST 클라이언트 — 디바이스 탐색과 MQTT JWT 프로비저닝.

EMQX는 익명 접속을 허용하지 않는다. 디바이스별 MQTT 커넥션은 그 디바이스만의
JWT(password)로 인증해야 하며, 이 JWT는 관리자 토큰으로 디바이스 시크릿을
발급받은 뒤 그 시크릿을 다시 디바이스 토큰으로 교환해야 얻을 수 있다
(세 번의 HTTP 왕복).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import requests

LOG = logging.getLogger("livesim.api")

TIMEOUT = 10
PAGE_SIZE = 200
MAX_PAGES = 50
MAINTENANCE = "MAINTENANCE"


class ApiError(RuntimeError):
    """API 호출이 복구 불가능하게 실패했을 때."""


@dataclass(frozen=True)
class DeviceRecord:
    """발행에 필요한 디바이스 정보. facility_type은 사이트에서 조인한 값."""

    device_id: str
    device_type: str
    site_id: str
    facility_type: str
    status: str


def _safe_error(res: requests.Response) -> str:
    """응답에서 진단에 필요한 부분만 뽑는다. 제출값은 절대 포함하지 않는다.

    422의 fieldErrors[].rejectedValue에는 제출한 비밀번호가 그대로 담겨 온다.
    본문을 통째로 로그에 남기면 평문 비밀번호가 남는다.
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


class AdminApi:
    """관리자 계정으로 동작하는 백엔드 클라이언트.

    access token은 1시간이면 만료된다. 24시간 운영 중에 반드시 한 번은 만료를
    맞으므로, 401을 받으면 한 번 재로그인하고 같은 요청을 다시 보낸다.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        session: requests.Session | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.session = session or requests.Session()
        self.token: str | None = None

    # ---- HTTP -----------------------------------------------------------

    def _send(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None,
        body: dict[str, Any] | None,
        auth: bool,
    ) -> requests.Response:
        headers = {}
        if auth and self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            return self.session.request(
                method,
                f"{self.base_url}{path}",
                params=params,
                json=body,
                headers=headers,
                timeout=TIMEOUT,
            )
        except requests.RequestException as exc:
            # 백엔드가 아직 안 떴거나 주소가 틀린 것은 흔한 운영 상황이다.
            # 여기서 ApiError로 바꿔야 호출자가 트레이스백 대신 원인 한 줄을
            # 보고, 디바이스별 프로비저닝 실패는 백오프 재시도로 흡수된다.
            raise ApiError(
                f"백엔드 통신 실패 ({self.base_url}{path}): {exc}"
            ) from exc

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> requests.Response:
        res = self._send(method, path, params, body, auth)
        if auth and res.status_code == 401:
            LOG.info("관리자 토큰 만료 감지 — 재로그인 후 재시도 (%s)", path)
            self.login()
            res = self._send(method, path, params, body, auth)
        return res

    def login(self) -> None:
        res = self._send(
            "POST",
            "/auth/login",
            None,
            {"username": self.username, "password": self.password},
            auth=False,
        )
        if res.status_code != 200:
            raise ApiError(f"관리자 로그인 실패 ({res.status_code}): {_safe_error(res)}")
        data = res.json()
        self.token = data["accessToken"]
        role = str(data.get("role", ""))
        if "ADMIN" not in role.upper():
            # 역할 이름은 백엔드 버전마다 늘어난다(ADMIN/SUPER_ADMIN 등). 하드
            # 매칭으로 막지 않고, 권한 부족은 실제 호출의 403으로 드러나게 둔다.
            LOG.warning(
                "role=%s — 관리자 계정이 아닐 수 있습니다 (디바이스 시크릿 발급 권한 필요)",
                role or "(없음)",
            )

    def _list_all(self, path: str) -> list[dict]:
        """ReadableList envelope를 페이지 끝까지 순회해 records를 모은다."""
        records: list[dict] = []
        page = 0
        while page < MAX_PAGES:
            res = self._request(
                "GET", path, params={"page": page, "pageSize": PAGE_SIZE}
            )
            if res.status_code != 200:
                raise ApiError(f"{path} 조회 실패 ({res.status_code}): {_safe_error(res)}")
            body = res.json()
            if not isinstance(body, dict):
                raise ApiError(
                    f"{path} 응답이 envelope가 아닙니다 (백엔드 계약 변경 가능성)"
                )
            items = body.get("records") or []
            records.extend(items)
            page += 1
            total_pages = body.get("totalPages")
            if not items or not isinstance(total_pages, int) or page >= total_pages:
                # 빈 페이지에서도 멈춘다 — totalPages가 실제 데이터보다 크게
                # 내려오면 무한히 같은 빈 페이지를 재요청하게 된다.
                return records
        raise ApiError(f"{path} 목록이 {MAX_PAGES}페이지를 넘습니다 — 조회 로직 확인 필요")

    # ---- 디바이스 탐색 ---------------------------------------------------

    def list_devices(self) -> list[dict]:
        return self._list_all("/admin/devices")

    def list_sites(self) -> list[dict]:
        return self._list_all("/admin/sites")

    def resolve_devices(
        self, exclude: tuple[str, ...] = (), max_devices: int = 0
    ) -> list[DeviceRecord]:
        """발행 대상 디바이스를 고른다.

        status가 MAINTENANCE인 디바이스만 뺀다. OFFLINE은 제외하지 않는다 —
        장시간 중단 후 재시작하면 백엔드 health checker가 전부 OFFLINE으로
        내려놓기 때문에, ONLINE만 고르면 아무 디바이스도 잡히지 않는다.
        OFFLINE 디바이스에 발행하면 적재 트리거가 다시 ONLINE으로 되살린다 —
        이것이 의도된 복구 경로다.
        """
        facility_by_site = {
            site.get("siteId"): site.get("facilityType")
            for site in self.list_sites()
        }
        excluded = set(exclude)

        resolved: list[DeviceRecord] = []
        for device in self.list_devices():
            device_id = device.get("deviceId")
            if not device_id or device_id in excluded:
                continue
            if str(device.get("status", "")).upper() == MAINTENANCE:
                continue
            site_id = device.get("siteId")
            facility_type = facility_by_site.get(site_id)
            if not site_id or not facility_type:
                # 토픽의 facility 세그먼트를 추측하면 EMQX 룰이 매칭되지 않아
                # 발행은 성공하는데 적재는 안 되는 조용한 유실이 된다.
                LOG.warning("사이트를 확인할 수 없어 제외: %s (siteId=%s)", device_id, site_id)
                continue
            resolved.append(
                DeviceRecord(
                    device_id=device_id,
                    device_type=str(device.get("deviceType") or "FIXED").upper(),
                    site_id=str(site_id),
                    facility_type=str(facility_type).upper(),
                    status=str(device.get("status") or "").upper(),
                )
            )

        resolved.sort(key=lambda item: item.device_id)
        if max_devices > 0:
            resolved = resolved[:max_devices]
        return resolved

    # ---- 프로비저닝 ------------------------------------------------------

    def issue_device_secret(self, device_id: str) -> str:
        """이미 등록된 디바이스의 시크릿을 발급받는다 (디바이스 생성은 하지 않는다)."""
        res = self._request("POST", f"/admin/devices/{device_id}/secret")
        if res.status_code not in (200, 201):
            raise ApiError(
                f"디바이스 시크릿 발급 실패 ({device_id}, {res.status_code}): "
                f"{_safe_error(res)}"
            )
        return res.json()["secret"]

    def exchange_device_token(self, device_id: str, secret: str) -> str:
        """시크릿을 디바이스 JWT로 교환한다.

        요청은 snake_case, 응답 키도 access_token으로 admin 토큰 응답과
        케이스가 다르다. 인증 헤더 없이 호출한다 — device_id/secret 자체가
        자격증명이며, 만료된 admin 토큰을 실어 보내면 오히려 401이 난다.
        """
        res = self._request(
            "POST",
            "/auth/device/token",
            body={"device_id": device_id, "secret": secret},
            auth=False,
        )
        if res.status_code != 200:
            raise ApiError(
                f"디바이스 토큰 교환 실패 ({device_id}, {res.status_code}): "
                f"{_safe_error(res)}"
            )
        return res.json()["access_token"]

    def provision_device_token(self, device_id: str) -> str:
        """시크릿 발급 + 토큰 교환을 묶어 디바이스 MQTT용 JWT를 반환한다."""
        return self.exchange_device_token(
            device_id, self.issue_device_secret(device_id)
        )
