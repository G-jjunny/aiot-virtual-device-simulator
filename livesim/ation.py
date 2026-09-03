"""에이티온(외부 벤더) HTTP 수집 경로 — 웨어러블 생체 데이터 송신.

MQTT 경로가 "우리가 만든 측정기가 우리 브로커로 직접 발행한다"면, 이 경로는
"벤더(에이티온) 서버가 우리 백엔드로 HTTP push한다"를 모의한다. 실제 웨어러블은
우리 브로커에 붙지 않으므로, 규격·필드·인증이 전부 다르다.

- 엔드포인트: ``POST {API_BASE_URL}/v1/biometric/ingest``
- **인증 없음.** 백엔드 v0.1.11이 X-API-KEY를 걷어내고 발신 IP 화이트리스트로
  바꿨다(``AtionIpWhitelistFilter``). 그래서 이 경로의 기기에는 secret도, 디바이스
  JWT도, MQTT 커넥션도 없다. 허용 목록 밖의 발신지는 403이다.
- 필드명은 에이티온 규격서 V1.1 원안 그대로다 — 공백이 들어간 ``"ENV TEMPERATURE"``·
  ``"ENV HUMIDITY"``, 혼자 소문자인 ``battery_percent``까지. 우리 관례로 고치면
  백엔드가 그 값을 못 읽는다(백엔드 DTO는 @JsonProperty로 원안 키에 붙어 있다).

계약 정본은 백엔드의 골든 픽스처
``aiot-be/aiot-api/src/test/resources/ation/golden-request.json``이다.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from livesim.api import ApiError, safe_error
from livesim.config import DeviceCredential
from livesim.payload import apply_overrides
from livesim.profiles import (
    DEFAULT_PROFILE,
    Profile,
    evaluate,
    sensor_value,
    unit_hash,
)

LOG = logging.getLogger("livesim.ation")

INGEST_PATH = "/v1/biometric/ingest"
TIMEOUT = 10
OK_STATUSES = (200, 201)

PHONE_NUMBER = "nothing"
"""규격 예시가 그대로 쓰는 값. 백엔드는 존재만 검증하고 저장하지 않는다
(실번호가 올 수 있는 필드라 가명화 없이 보관하지 않는다) — 시뮬레이터가 굳이
그럴듯한 번호를 지어낼 이유가 없다."""

FW_VERSION = "L_W100_AP_V1.0.00-SIM"
"""벤더 펌웨어 표기 형태를 따르되 끝을 SIM으로 둔다. 형태를 맞춰야 FE·백엔드가
실물과 같게 다루고, 접미어가 있어야 적재된 행이 시뮬레이터 산출물임을 알 수 있다."""

KST = timezone(timedelta(hours=9))

# 오버라이드(버스트)가 닿는 파형 필드 — livesim 센서 이름 → 에이티온 키.
#
# 걸음·수면·배터리·위치는 여기 없다. 그것들은 일주기 파형이 아니라 누적·상태
# 모델이라 "지금 값을 150으로" 같은 목표치가 의미를 갖지 않는다.
OVERRIDABLE_FIELDS = ("heart_rate", "spo2", "skin_temp", "temp", "humi")


# ---- 혈압 -----------------------------------------------------------------

BP_SYS_PROFILE = Profile(118.0, 10.0, 4.0, 16, 70.0, 200.0, decimals=0)
BP_DIA_PROFILE = Profile(76.0, 7.0, 3.0, 16, 40.0, 130.0, decimals=0)
MIN_PULSE_PRESSURE = 25.0
"""수축기와 이완기의 최소 간격(맥압). 사람에게 있을 수 없는 조합을 막는 하한이다."""


def blood_pressure(ts: datetime, seed: int = 0) -> tuple[int, int]:
    """(수축기, 이완기). 항상 수축기 > 이완기이며 맥압 25 이상을 보장한다.

    두 파형을 따로 뽑기만 하면 노이즈가 어긋나는 순간 이완기가 수축기를 넘어선다.
    백엔드는 각 값의 범위만 검증하므로(둘의 관계는 보지 않는다) 그런 행이 그대로
    적재되고, 나중에 데이터에서 걸러낼 방법이 없다. 그래서 수축기를 먼저 정하고
    이완기를 그 아래로 눌러 담는다 — 반대로 하면 수축기가 파형을 벗어난다.

    **현재 파라미터에서는 이 클램프가 발동하지 않는다.** 두 프로필의 peak_hour가
    같아 파형이 같은 위상으로 움직이고, 노이즈 최악까지 더해도 간격이 32~51로
    벌어져 있기 때문이다. 그래도 남겨 두는 이유는 base·amplitude·peak_hour 중
    하나만 손대도 곧바로 필요해지는 방어선이라서다 — 그때 동작하는지는
    test_ation.py의 클램프 테스트가 프로필을 바꿔 끼워 직접 확인한다.
    """
    systolic = evaluate(BP_SYS_PROFILE, "bp_sys", ts, seed)
    diastolic = min(
        evaluate(BP_DIA_PROFILE, "bp_dia", ts, seed), systolic - MIN_PULSE_PRESSURE
    )
    return int(round(systolic)), int(round(diastolic))


# ---- 걸음 수 ---------------------------------------------------------------

STEPS_MIN_DAILY = 3000
STEPS_MAX_DAILY = 12000

# 시간대별 활동 가중치(0시~23시). 07~22시가 활동 시간대이고 야간은 거의 멈춘다
# (뒤척임 정도). 합계 대비 비율이므로 절대값 자체에는 의미가 없다.
STEP_WEIGHTS = (
    0.05, 0.05, 0.05, 0.05, 0.05, 0.05, 0.05,   # 00~06시 야간 (뒤척임 정도)
    6.0, 5.0, 4.0, 4.0, 5.0, 8.0, 7.0,          # 07~13시 출근·오전·점심
    4.0, 4.0, 5.0, 7.0, 9.0, 8.0, 6.0, 4.0,     # 14~21시 오후·퇴근·저녁
    0.05, 0.05,                                 # 22~23시 야간
)
_STEP_TOTAL_WEIGHT = sum(STEP_WEIGHTS)


def daily_steps(device_id: str, ts: datetime) -> int:
    """그 기기의 그날 총 걸음 수. 기기·날짜로 결정되며 3000~12000 사이."""
    span = STEPS_MAX_DAILY - STEPS_MIN_DAILY
    return int(STEPS_MIN_DAILY + span * unit_hash(f"steps|{device_id}|{ts.date()}"))


def step_count(device_id: str, ts: datetime) -> int:
    """자정 기준 누적 걸음 수.

    실제 단말이 보내는 값이 그날의 **누적 카운터**라 자정에 0으로 리셋되고 하루
    동안 단조 증가한다. 매 전송 독립적인 파형으로 만들면 걸음 수가 줄어드는
    구간이 생겨, 실물이라면 있을 수 없는 데이터가 된다.
    """
    total = daily_steps(device_id, ts)
    elapsed = sum(STEP_WEIGHTS[: ts.hour])
    elapsed += STEP_WEIGHTS[ts.hour] * (ts.minute * 60 + ts.second) / 3600.0
    return int(total * elapsed / _STEP_TOTAL_WEIGHT)


# ---- 수면 -----------------------------------------------------------------

SLEEP_START_HOUR = 23
SLEEP_END_HOUR = 7
SLEEP_WINDOW_MINUTES = ((SLEEP_END_HOUR - SLEEP_START_HOUR) % 24) * 60  # 480
SLEEP_MIN_MINUTES = 330
SLEEP_MAX_MINUTES = 480
WAKEUP_RATIO = (0.03, 0.08)
"""수면 중 각성 시간의 비율. 수면 총량이 아니라 그 안의 뒤척임이므로 작은 값이다."""


def _sleep_session_date(ts: datetime):
    """이 시각이 속한 수면 세션의 시작 날짜 (23시 시작 기준).

    23시 이전이면 어젯밤 세션의 결과를 계속 보고하는 중이다 — 실제 웨어러블도
    낮 동안 "어젯밤 몇 시간 잤다"를 유지해 보여준다.
    """
    return ts.date() if ts.hour >= SLEEP_START_HOUR else (ts - timedelta(days=1)).date()


def _sleep_elapsed_minutes(ts: datetime) -> float:
    hour = ts.hour
    if hour >= SLEEP_START_HOUR or hour < SLEEP_END_HOUR:
        return ((hour - SLEEP_START_HOUR) % 24) * 60 + ts.minute + ts.second / 60.0
    return float(SLEEP_WINDOW_MINUTES)  # 주간 — 전날 밤 총량을 그대로 유지


def sleep_minutes(device_id: str, ts: datetime) -> tuple[int, int]:
    """(수면 분, 각성 분). 야간에 누적 증가하고 주간에는 그 밤의 총량을 유지한다."""
    session = _sleep_session_date(ts)
    span = SLEEP_MAX_MINUTES - SLEEP_MIN_MINUTES
    total = SLEEP_MIN_MINUTES + span * unit_hash(f"sleep|{device_id}|{session}")
    progress = min(1.0, _sleep_elapsed_minutes(ts) / SLEEP_WINDOW_MINUTES)
    sleep = int(total * progress)

    low, high = WAKEUP_RATIO
    ratio = low + (high - low) * unit_hash(f"wakeup|{device_id}|{session}")
    return sleep, int(round(sleep * ratio))


# ---- 배터리 ----------------------------------------------------------------

BATTERY_FULL = 100
BATTERY_FLOOR = 20
BATTERY_CYCLE_HOURS = 96.0
_BATTERY_EPOCH = datetime(2026, 1, 1)


def battery_percent(device_id: str, ts: datetime) -> int:
    """100에서 서서히 줄다 20에 닿으면 100으로 돌아간다(충전 모사).

    기기마다 위상이 다르다 — 같은 시점에 플릿 전체가 나란히 방전되면 배터리
    경고 UI를 시험할 때 "전부 빨강" 아니면 "전부 정상"밖에 못 만든다.
    """
    phase = unit_hash(f"battery|{device_id}") * BATTERY_CYCLE_HOURS
    hours = (ts - _BATTERY_EPOCH).total_seconds() / 3600.0
    position = ((hours + phase) % BATTERY_CYCLE_HOURS) / BATTERY_CYCLE_HOURS
    return int(round(BATTERY_FULL - (BATTERY_FULL - BATTERY_FLOOR) * position))


# ---- 위치 -----------------------------------------------------------------

SEOUL_LAT = 37.5665
SEOUL_LON = 126.9780
DEVICE_SPREAD_DEG = 0.05
"""인벤토리에 좌표가 없을 때 기기를 흩뿌리는 범위. 서울 시내 정도의 폭이다."""
JITTER_DEG = 0.0003
"""매 전송 흔들림. 측위 오차만큼 좌표가 미세하게 움직이는 것을 모의한다."""
ACCURACY_MIN = 10
ACCURACY_MAX = 50


def base_position(credential: DeviceCredential) -> tuple[float, float]:
    """기기의 고정 기준 좌표. 인벤토리에 적었으면 그 값이 우선한다."""
    if credential.latitude is not None and credential.longitude is not None:
        return credential.latitude, credential.longitude
    device_id = credential.device_id
    return (
        SEOUL_LAT + (unit_hash(f"lat|{device_id}") * 2 - 1) * DEVICE_SPREAD_DEG,
        SEOUL_LON + (unit_hash(f"lon|{device_id}") * 2 - 1) * DEVICE_SPREAD_DEG,
    )


def _jitter(key: str, ts: datetime, seed: int) -> float:
    """[-1, 1] 결정적 유사 난수. 파형이 아닌 값(좌표·정확도)의 흔들림에 쓴다."""
    return unit_hash(f"{key}|{ts.isoformat()}|{seed}") * 2.0 - 1.0


def positioning(
    credential: DeviceCredential, ts: datetime, seed: int = 0
) -> dict[str, Any]:
    """규격의 측위 성공 블록. 시뮬레이터는 측위에 실패하지 않는다.

    실패형(result_code != 0)은 백엔드가 "위치만 생략"으로 흡수하도록 이미
    설계돼 있어, 여기서 굳이 만들지 않아도 그 경로가 시험되지 않는 것은 아니다.
    """
    latitude, longitude = base_position(credential)
    latitude = round(latitude + _jitter("poslat", ts, seed) * JITTER_DEG, 5)
    longitude = round(longitude + _jitter("poslon", ts, seed) * JITTER_DEG, 5)
    span = ACCURACY_MAX - ACCURACY_MIN + 1
    accuracy = ACCURACY_MIN + int(unit_hash(f"acc|{ts.isoformat()}|{seed}") * span)
    return {
        "location": {
            "result_code": 0,
            "result_msg": "OK",
            "result_data": {
                "pos_info": {
                    "latitude": latitude,
                    "longitude": longitude,
                    "accuracy_h": min(accuracy, ACCURACY_MAX),
                    # 정밀값의 저정밀 중복. 백엔드는 무시하지만 규격에 있는 키라
                    # 형태를 맞춘다 (없다고 거부되지는 않지만, 실물과 다른 모양을
                    # 보내면 이 경로로 무엇이 오는지 확인할 수 없다).
                    "latitude_coarse": round(latitude, 3),
                    "longitude_coarse": round(longitude, 3),
                    "altitude": 0,
                    "accuracy_v": 0,
                    "floor": 0,
                }
            },
        }
    }


# ---- 통계 -----------------------------------------------------------------

STAT_NAME = "PPG_HR"
STAT_SAMPLE_SECONDS = 60.0
MAX_STAT_SAMPLES = 60
"""창이 아무리 길어도 샘플 수를 여기서 끊는다 — 장시간 정지 후 첫 전송에서
통계 계산이 폭주하지 않게."""


def _sample_points(start: datetime, end: datetime) -> list[datetime]:
    span = (end - start).total_seconds()
    if span <= 0:
        return [end]
    step = max(STAT_SAMPLE_SECONDS, span / MAX_STAT_SAMPLES)
    points: list[datetime] = []
    offset = 0.0
    while offset < span:
        points.append(start + timedelta(seconds=offset))
        offset += step
    points.append(end)
    return points


def heart_rate_statistic(
    ts: datetime,
    since: datetime | None,
    seed: int = 0,
    preset: str = DEFAULT_PROFILE,
    facility_type: str | None = None,
) -> dict[str, Any]:
    """이번 전송 창(직전 전송 ~ 지금)의 PPG_HR 통계 1건.

    단말은 전송 주기보다 자주 측정하므로 창 안의 파형을 여러 번 샘플링해 통계를
    만든다. 전송 시점 값 하나로 count=1을 보내면 통계 자체가 의미를 잃는다 —
    min·max·average가 전부 같은 값이 되어 실제 단말이 주는 변동폭이 사라진다.

    첫 전송(직전이 없음)은 창이 없으므로 지금 값 1건짜리 통계가 된다.
    """
    start = since if since is not None else ts
    samples = [
        sensor_value("heart_rate", point, seed, preset, facility_type)
        for point in _sample_points(start, ts)
    ]
    return {
        "name": STAT_NAME,
        "count": len(samples),
        "average": round(sum(samples) / len(samples), 1),
        "min": min(samples),
        "max": max(samples),
        "window_start": epoch_millis(start),
        "window_end": epoch_millis(ts),
    }


# ---- 페이로드 --------------------------------------------------------------


def epoch_millis(ts: datetime) -> int:
    """naive KST 벽시계를 UNIX epoch 밀리초로 바꾼다.

    ⚠ **이 경로의 적재 시각은 MQTT 경로와 기준이 다르다.** 백엔드는 이 값을
    ``EpochMillis``로 **UTC 기준** LocalDateTime에 넣으므로, 여기서 보낸 실제
    시점이 UTC 벽시계로 적힌다. MQTT 경로는 naive KST 숫자가 그대로 적재되므로
    (§7) 같은 순간에 보낸 두 경로의 ``captured_at``이 9시간 어긋난다.

    **그래도 보정하지 않는다.** 규격이 epoch(모호하지 않은 시점)를 요구하는데
    임의로 9시간을 더하면 규격을 어기는 값이 되고, 백엔드가 기준을 고치는 날
    두 번 틀린다. 차이는 문서로 남기고 값은 정직하게 보낸다.
    """
    if ts.tzinfo is not None:
        raise ValueError(
            f"ts는 naive datetime이어야 합니다 (KST로 간주). 받은 값: {ts!r}"
        )
    return int(ts.replace(tzinfo=KST).timestamp() * 1000)


def build_reading(
    ts: datetime, seed: int, preset: str, facility_type: str | None
) -> dict[str, float]:
    """오버라이드가 닿는 파형 값만 livesim 센서 이름으로 만든다.

    에이티온 키로 곧바로 만들지 않는 이유: 버스트 목표치(`ctl burst --set`)와
    클램프가 전부 livesim 센서 이름 기준이라, 여기서 이름을 바꿔 버리면
    `apply_overrides`가 아무 항목도 찾지 못해 조용히 무시된다.
    """
    return {
        name: sensor_value(name, ts, seed, preset, facility_type)
        for name in OVERRIDABLE_FIELDS
    }


def build_payload(
    credential: DeviceCredential,
    ts: datetime,
    seed: int = 0,
    preset: str = DEFAULT_PROFILE,
    since: datetime | None = None,
    overrides: dict[str, float] | None = None,
    message_id: str | None = None,
) -> dict[str, Any]:
    """규격 1건. 키 구성은 골든 픽스처와 같아야 한다.

    ``quality``에 해당하는 필드가 규격에 없다 — 백엔드 매퍼가 이 경로의 행을
    무조건 ``QualityFlag.OK``로 적재한다. 그래서 이 경로에서는 품질 축을 조작할
    수 없고, 억지로 필드를 만들지 않는다(계약에 없는 키는 백엔드가 버린다).
    """
    reading = apply_overrides(
        build_reading(ts, seed, preset, credential.facility_type), overrides
    )
    systolic, diastolic = blood_pressure(ts, seed)
    sleep, wakeup = sleep_minutes(credential.device_id, ts)
    return {
        "MESSAGE_ID": message_id or str(uuid.uuid4()),
        "device": {
            "DEVICE_ID": credential.device_id,
            "PHONE_NUMBER": PHONE_NUMBER,
            "FW_VER": FW_VERSION,
            "PPG_HR": reading["heart_rate"],
            # 규격 예시가 정수다. 백엔드는 BigDecimal로 받지만, 실물이 정수를
            # 보내는데 소수를 흘리면 이 경로로 무엇이 오는지 알 수 없게 된다.
            "PPG_SPO2": int(round(reading["spo2"])),
            "IMU_STEP_CNT": step_count(credential.device_id, ts),
            "BP_SYS": systolic,
            "BP_DIA": diastolic,
            # skin_temp 프로필은 소수 1자리지만 컬럼이 NUMERIC(5,2)라 2자리까지 담긴다.
            "TEMPERATURE": round(reading["skin_temp"], 2),
            # 공백이 든 키다. 원안 그대로 보낸다 — 고치면 백엔드가 못 읽는다.
            "ENV TEMPERATURE": reading["temp"],
            "ENV HUMIDITY": reading["humi"],
            "SLEEP_DURATION": sleep,
            "WAKEUP_DURATION": wakeup,
            "battery_percent": battery_percent(credential.device_id, ts),
            "MEASURED_AT": epoch_millis(ts),
        },
        "positioning": positioning(credential, ts, seed),
        "statistics": [
            heart_rate_statistic(ts, since, seed, preset, credential.facility_type)
        ],
    }


# ---- 송신 -----------------------------------------------------------------

_FORBIDDEN_HINT = (
    "발신 IP가 백엔드의 ATION_ALLOWED_IPS 화이트리스트에 없습니다 "
    "(이 경로는 헤더 인증이 없어 IP로만 통과합니다) — "
)


def send(
    api_base_url: str,
    payload: dict[str, Any],
    session: requests.Session | None = None,
    timeout: int = TIMEOUT,
) -> None:
    """수집 1건을 보낸다. 200/201이 아니면 예외.

    인증 헤더를 붙이지 않는다 — 이 경로에는 인증 헤더 자체가 없다. 실물 송신측도
    아무 헤더를 붙이지 않으므로, 여기서 뭔가를 붙이면 실물과 다른 것을 시험하게 된다.
    """
    caller = session or requests
    url = f"{api_base_url.rstrip('/')}{INGEST_PATH}"
    device_id = payload.get("device", {}).get("DEVICE_ID", "?")
    try:
        res = caller.post(url, json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise ApiError(f"에이티온 수집 전송 실패 ({device_id}, {url}): {exc}") from exc

    if res.status_code not in OK_STATUSES:
        # 4xx는 재시도해도 같은 답이 온다. 특히 403은 이 기기의 문제가 아니라
        # 발신 호스트 전체의 문제라, 원인을 여기서 못 박아야 사람이 엉뚱한
        # 기기 설정을 뒤지지 않는다.
        hint = _FORBIDDEN_HINT if res.status_code == 403 else ""
        raise ApiError(
            f"에이티온 수집 거부 ({device_id}, {res.status_code}): "
            f"{hint}{safe_error(res)}",
            status=res.status_code,
        )


@dataclass
class AtionDevice:
    """벤더 HTTP 경로로 생체 데이터를 보내는 웨어러블 1대.

    ``LiveDevice``와 달리 커넥션도, 오프라인 버퍼도, batch 재전송도 없다. HTTP는
    매 전송이 독립이고 이 규격에는 밀린 측정을 몰아 보내는 배열 형태가 없다 —
    그래서 이 경로의 단절은 "나중에 메울 수 있는 지연"이 아니라 그냥 유실이다.
    """

    credential: DeviceCredential
    api_base_url: str
    session: requests.Session | None = field(default=None, repr=False)
    profile: str = DEFAULT_PROFILE
    last_sent_at: datetime | None = None
    sent: int = 0

    @property
    def device_id(self) -> str:
        return self.credential.device_id

    def publish(
        self,
        ts: datetime,
        seed: int = 0,
        overrides: dict[str, float] | None = None,
    ) -> bool:
        payload = build_payload(
            self.credential,
            ts,
            seed=seed,
            preset=self.profile,
            since=self.last_sent_at,
            overrides=overrides,
        )
        send(self.api_base_url, payload, self.session)
        # 전송이 실패하면 창을 넘기지 않는다. 다음 성공한 전송의 통계가 실패로
        # 비어 있던 구간까지 덮어야 그 시간의 심박이 통계에서 사라지지 않는다.
        self.last_sent_at = ts
        self.sent += 1
        return True
