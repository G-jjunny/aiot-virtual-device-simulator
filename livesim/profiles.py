"""센서 파형 생성 — 일주기 + 결정적 노이즈. 순수 함수이며 부작용이 없다."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, replace
from datetime import datetime


@dataclass(frozen=True)
class Profile:
    base: float
    amplitude: float
    noise: float
    peak_hour: int
    minimum: float
    maximum: float
    decimals: int = 2


# IAQ 18종 (백엔드 V5 마이그레이션 + 업로드 DTO 검증 범위 기준). 각 profile의
# minimum/maximum은 DTO의 @DecimalMin/@DecimalMax 안에 들어야 발행이 422가
# 안 난다. decimals는 DB 컬럼(NUMERIC(x,y))과 맞춘다 — 컬럼보다 자릿수가 많으면
# 저장 시 반올림돼 "보낸 값과 다르다"는 오탐이 난다.
SENSOR_PROFILES: dict[str, Profile] = {
    # --- 공통 10종 (고정형/보급형 공통) ---
    "temp":        Profile(23.5, 3.5,  0.8, 15, -20.0, 60.0),      # -40~125 C
    "humi":        Profile(52.0, 10.0, 3.0, 5,  0.0,   100.0),     # 0~100 %
    "pm10":        Profile(32.0, 14.0, 5.0, 9,  0.0,   600.0),     # 0~1000
    "pm25":        Profile(18.0, 9.0,  3.0, 9,  0.0,   500.0),     # 0~1000
    "pm1_0":       Profile(12.0, 6.0,  2.0, 9,  0.0,   400.0),     # 0~1000
    "co2":         Profile(700.0, 200.0, 40.0, 14, 400.0, 10000.0),  # 400~10000 ppm
    "tvoc":        Profile(150.0, 80.0, 20.0, 14, 0.0, 99999.0, decimals=1),  # ppb
    "noise":       Profile(45.0, 12.0, 4.0, 13, 20.0,  120.0, decimals=1),
    "odor":        Profile(0.05, 0.03, 0.02, 14, 0.0,  10.0, decimals=3),   # 0~10 ppm
    "lux":         Profile(450.0, 350.0, 100.0, 12, 0.0, 17867.0),  # 0~17867 lx
    # --- 고정형 전용 8종 (보급형은 미탑재 = NULL) ---
    "co":          Profile(0.2, 0.15, 0.05, 9,  0.0, 10.0, decimals=3),    # 0~10 ppm
    "hcho":        Profile(0.04, 0.02, 0.01, 14, 0.0, 3.0, decimals=3),    # 0~3 ppm
    "radon":       Profile(1.3, 0.6, 0.2, 4, 0.2, 99.9),                   # 0.2~99.9 pCi/L
    "no2":         Profile(0.02, 0.01, 0.005, 9, 0.0, 5.0, decimals=3),    # 0~5 ppm
    "h2s":         Profile(0.01, 0.006, 0.003, 9, 0.0, 5.0, decimals=3),   # 0~5 ppm
    "nh3":         Profile(0.01, 0.006, 0.003, 9, 0.0, 10.0, decimals=3),  # 0~10 ppm
    "o3":          Profile(0.02, 0.01, 0.005, 14, 0.0, 5.0, decimals=3),   # 0~5 ppm
    "o2":          Profile(20.9, 0.2, 0.05, 12, 0.0, 25.0),                # 0~25 %vol
    # --- 생체 (웨어러블) ---
    "heart_rate":  Profile(74.0, 12.0, 5.0, 16, 40.0,  200.0, decimals=1),
    "spo2":        Profile(97.5, 1.0,  0.6, 16, 80.0,  100.0, decimals=1),
    "skin_temp":   Profile(33.5, 0.8,  0.3, 16, 25.0,  42.0, decimals=1),
}

# 공통 10종: 고정형·보급형 모두 측정
COMMON_FIELDS = (
    "temp", "humi", "pm10", "pm25", "pm1_0", "co2", "tvoc", "noise", "odor", "lux",
)
# 고정형 전용 8종: 보급형 디바이스는 미탑재
FIXED_EXTENDED_FIELDS = ("co", "hcho", "radon", "no2", "h2s", "nh3", "o3", "o2")
# 생체 3종: 웨어러블 전용
BIOMETRIC_FIELDS = ("heart_rate", "spo2", "skin_temp")

# 고정형이 측정하는 대기질 전체 = 공통 + 확장
AIR_QUALITY_FIELDS = COMMON_FIELDS + FIXED_EXTENDED_FIELDS

DEVICE_FIELDS: dict[str, tuple[str, ...]] = {
    "FIXED": COMMON_FIELDS + FIXED_EXTENDED_FIELDS,
    "PORTABLE": COMMON_FIELDS,
    "WEARABLE": BIOMETRIC_FIELDS,
}


def _unit_noise(name: str, ts: datetime, seed: int) -> float:
    """[-1, 1] 범위의 결정적 유사 난수."""
    key = f"{name}|{ts.isoformat()}|{seed}".encode()
    digest = hashlib.sha256(key).digest()
    raw = int.from_bytes(digest[:8], "big") / float(1 << 64)  # [0, 1)
    return raw * 2.0 - 1.0


GOOD = "good"
MODERATE = "moderate"
BAD = "bad"
VERY_BAD = "very_bad"
DEFAULT_PROFILE = GOOD

# 환경 프리셋 — 기기가 놓인 **상시** 환경 등급. 버스트(일시 이벤트)와 달리
# 기간이 없다.
#
# 차등 테이블이다: {지표: (base, amplitude)}만 갈아끼우고 noise·peak_hour·
# min/max·decimals는 good의 것을 그대로 물려받는다. 물리 클램프를 건드리지
# 않아야 업로드 DTO 검증(422)이 그대로 통과한다.
#
# good은 비어 있다 = SENSOR_PROFILES 원본. 기존 동작을 바꾸지 않기 위해서다.
ENVIRONMENT_PRESETS: dict[str, dict[str, tuple[float, float]]] = {
    GOOD: {},
    MODERATE: {
        # 등급 경계를 걸치게 둔다 — 시간대에 따라 '좋음'과 '나쁨' 사이를 오간다.
        "pm25": (40.0, 12.0),
        "pm10": (70.0, 20.0),
        "co2": (1100.0, 250.0),
        "tvoc": (350.0, 120.0),
        "hcho": (0.08, 0.03),
    },
    BAD: {
        "pm25": (65.0, 15.0),
        "pm10": (110.0, 25.0),
        "co2": (1600.0, 300.0),
        "tvoc": (700.0, 200.0),
        "hcho": (0.13, 0.04),
        "co": (1.5, 0.5),
        "radon": (3.5, 1.0),
        # 생체는 미세하게만 — 오염 환경의 생리 반응이지 질병이 아니다.
        "heart_rate": (82.0, 12.0),
        "spo2": (96.0, 1.0),
    },
    VERY_BAD: {
        "pm25": (100.0, 25.0),
        "pm10": (170.0, 35.0),
        "co2": (2500.0, 500.0),
        "tvoc": (1200.0, 300.0),
        "hcho": (0.2, 0.06),
        "co": (3.0, 1.0),
        "radon": (5.0, 1.5),
        "no2": (0.08, 0.03),
        "o3": (0.07, 0.03),
        "h2s": (0.03, 0.01),
        "nh3": (0.04, 0.015),
        "heart_rate": (86.0, 12.0),
        "spo2": (95.0, 1.0),
    },
}

PRESET_NAMES = tuple(ENVIRONMENT_PRESETS)

# ---- 시설별 법정 밴드 보정 -------------------------------------------------
#
# 위 일반 기본표는 환경부 일반 기준으로 잡혀 있지만, FE의 등급 배지는 백엔드
# metric_grade_band의 **시설유형별 법정 3단계**로 매겨진다. 두 기준이 어긋나서
# 양방향 결함이 났다:
#   - 사무실 pm10 '나쁨' 경계는 200인데 very_bad(170±35)로도 대부분 못 넘는다.
#   - 어린이집 tvoc '나쁨' 경계는 106인데 good(150±80)이 상시 '나쁨'으로 뜬다.
#
# 그래서 프리셋의 의미를 다시 정의한다 —
#   **프리셋 이름 = 그 기기가 놓인 시설의 법정 밴드 기준 등급.**
#   good→'좋음' 안정, moderate→'보통' 중심, bad→'나쁨' 확실, very_bad→크게 초과.
#
# 밴드가 있는 지표만 시설별로 덮어쓰고, 밴드가 없는 지표는 일반 기본표를 그대로
# 쓴다(원시값 표시용). 여기서도 갈아끼우는 것은 base/amplitude뿐이다.
FACILITY_GRADE_BANDS: dict[str, dict[str, tuple[float, float | None]]] = {
    # (좋음 상한, 보통 상한) — 상한은 **이하 포함**, 보통 상한을 넘으면 '나쁨'.
    # 보통 상한이 None이면 2단계(적합/부적합) 지표라 '보통'이 없다.
    #
    # 출처: aiot-be v0.1.8, V6 마이그레이션의 metric_grade_band 시드 실측
    #       (V9는 id 전략만 바꾼다). 2026-08-24 스냅샷.
    # ⚠ 백엔드 밴드가 바뀌면 이 표와 아래 FACILITY_PRESETS를 함께 갱신해야 한다.
    #   여기 값은 런타임 판정에 쓰이지 않는다 — 등급은 백엔드가 매긴다. 이 표는
    #   프리셋이 목표 등급 구간을 지키는지 검증하는 기준(테스트 스펙)이다.
    "DAYCARE": {
        "pm25": (28.0, 35.0), "pm10": (60.0, 75.0), "co2": (800.0, 1000.0),
        "tvoc": (85.0, 106.0), "hcho": (0.0521, 0.0651), "no2": (0.04, 0.05),
        "radon": (3.2, 4.0), "co": (8.0, 10.0),
    },
    "WELFARE": {
        "pm25": (28.0, 35.0), "pm10": (60.0, 75.0), "co2": (800.0, 1000.0),
        "tvoc": (85.0, 106.0), "hcho": (0.0521, 0.0651), "no2": (0.04, 0.05),
        "radon": (3.2, 4.0), "co": (8.0, 10.0),
    },
    "SCHOOL": {
        # 경계는 어린이집·복지시설과 같고 집계 방식만 다르다(일평균·측정평균 등).
        # 순간값이 구간 안에 있으면 그 평균도 같은 구간이므로 보정은 공유한다.
        "pm25": (28.0, 35.0), "pm10": (60.0, 75.0), "co2": (800.0, 1000.0),
        "tvoc": (85.0, 106.0), "hcho": (0.0521, 0.0651), "no2": (0.04, 0.05),
        "radon": (3.2, 4.0), "co": (8.0, 10.0),
        "noise": (55.0, None), "o3": (0.06, None),
        # humi(40~70 좋음 / 30~40·70~80 보통 / 그 바깥 나쁨)는 양방향 밴드라
        # 이 (상한, 상한) 표로 표현할 수 없다. 아래 SCHOOL_HUMI_BAND 참고.
    },
    "OFFICE": {"pm10": (160.0, 200.0), "co": (8.0, 10.0)},
    "HOME": {"hcho": (0.171, None), "radon": (3.2, 4.0), "co": (8.0, 10.0)},
    # HOME_ELDERLY는 백엔드 시드에 밴드가 없다 → 보정 없이 일반 기본표를 쓴다.
}

SCHOOL_HUMI_BAND = (40.0, 70.0, 30.0, 80.0)
"""학교 습도 — (좋음 하한, 좋음 상한, 나쁨 하한 경계, 나쁨 상한 경계).

좋음 40~70(포함), 보통 30~40 미만·70 초과~80, 30 미만과 80 초과가 나쁨.
"""

# 등급 보장에서 **제외**하는 지표와 그 이유 (문서화된 예외):
#   co    — 물리 상한이 10 ppm(측정기 사양)인데 '나쁨'은 10 초과다. 어떤
#           프리셋으로도 '나쁨'에 도달할 수 없다. 발행 자체는 정상이다.
#   lux   — 조도는 오염도가 아니라 조명 상태 지표이고 일주기상 야간에는
#           반드시 어두워진다. '좋음'(사무실·학교 400 이상)을 24시간 유지하려면
#           밤에도 불이 켜진 방을 흉내내야 해서 오히려 비현실적이다.
#   temp  — 쾌적 지표. 학교 적합 구간 18~28℃ 안에 기본 파형이 이미 들어와 있고,
#           프리셋은 '오염도' 등급이므로 온도까지 밀어 올리지 않는다.

_PresetTable = dict[str, dict[str, tuple[float, float]]]


def _merge(base: _PresetTable, extra: _PresetTable) -> _PresetTable:
    merged: _PresetTable = {preset: dict(values) for preset, values in base.items()}
    for preset, values in extra.items():
        merged.setdefault(preset, {}).update(values)
    return merged


# 어린이집·복지시설·학교가 공유하는 엄격 시설 보정.
#
# 값은 "±진폭 + 노이즈까지 더한 최악 케이스에도 목표 등급 구간을 벗어나지
# 않는다"를 기준으로 잡았다. 노이즈가 밴드 폭에 비해 큰 지표(tvoc 노이즈 20 vs
# 보통 폭 21)가 있어서, moderate만은 **파형(base±amplitude) 기준**으로 보통에
# 들어가고 노이즈에 의한 경계 걸침은 허용한다 — 원래 moderate의 설계 의도다.
_STRICT: _PresetTable = {
    GOOD: {
        "pm25": (16.0, 7.0),     # 최악 26 ≤ 28
        "co2": (600.0, 130.0),   # 최악 770 ≤ 800 (하한 430 ≥ 400)
        "tvoc": (40.0, 15.0),    # 최악 75 ≤ 85
        "hcho": (0.026, 0.010),  # 최악 0.046 ≤ 0.0521
    },
    MODERATE: {
        "pm25": (31.5, 3.0),     # 파형 28.5~34.5
        "pm10": (67.0, 6.0),     # 파형 61~73
        "co2": (900.0, 80.0),    # 파형 820~980
        "tvoc": (95.0, 8.0),     # 파형 87~103
        "hcho": (0.059, 0.004),  # 파형 0.055~0.063
    },
    BAD: {
        "pm25": (50.0, 10.0),    # 최악 하한 37 > 35
        "pm10": (105.0, 20.0),   # 80 > 75
        "co2": (1300.0, 200.0),  # 1060 > 1000
        "tvoc": (200.0, 60.0),   # 120 > 106
        "hcho": (0.11, 0.02),    # 0.08 > 0.0651
        "radon": (5.2, 0.8),     # 4.2 > 4.0
    },
    VERY_BAD: {
        "pm25": (85.0, 20.0),
        "pm10": (150.0, 30.0),
        "co2": (2200.0, 400.0),
        "tvoc": (450.0, 120.0),
        "hcho": (0.15, 0.04),
        "radon": (6.0, 1.0),
        "no2": (0.095, 0.030),   # 0.06 > 0.05 (일반표 0.08±0.03은 '보통'에 걸린다)
    },
}

# 학교에만 있는 밴드: 습도(양방향)·소음(2단계)·오존(2단계).
# good/moderate에서도 덮어써야 한다 — 프리셋별 표는 서로 독립이라, 여기 없으면
# 일반 기본값(소음 45±12+4 → 최악 61 > 55)이 그대로 '나쁨'으로 샌다.
_SCHOOL_ONLY: _PresetTable = {
    GOOD: {"humi": (55.0, 9.0), "noise": (40.0, 9.0)},
    MODERATE: {"humi": (75.0, 1.0), "noise": (40.0, 9.0)},
    BAD: {"humi": (86.0, 2.0), "noise": (70.0, 8.0)},
    VERY_BAD: {"humi": (90.0, 2.0), "noise": (85.0, 8.0), "o3": (0.10, 0.03)},
}

FACILITY_PRESETS: dict[str, _PresetTable] = {
    "DAYCARE": _STRICT,
    "WELFARE": _STRICT,
    "SCHOOL": _merge(_STRICT, _SCHOOL_ONLY),
    # 사무실은 pm10만 밴드가 있다(경계 160/200). pm25·co2·tvoc는 밴드가 없어
    # 일반 기본표 값을 그대로 둔다 — 등급이 아니라 원시값으로 읽히는 지표다.
    "OFFICE": {
        MODERATE: {"pm10": (180.0, 10.0)},   # 최악 165~195, 보통(160~200) 안
        BAD: {"pm10": (230.0, 20.0)},        # 최악 하한 205 > 200
        VERY_BAD: {"pm10": (260.0, 40.0)},   # 215 > 200
    },
    # 가정은 hcho(2단계 신축공동주택 기준)·radon만. hcho에는 '보통'이 없어
    # moderate는 '적합'에 머문다.
    "HOME": {
        BAD: {"hcho": (0.24, 0.04), "radon": (5.2, 0.8)},   # 0.19 > 0.171
        VERY_BAD: {"hcho": (0.30, 0.08), "radon": (6.0, 1.0)},
    },
}


def _override_for(
    name: str, preset: str, facility_type: str | None
) -> tuple[float, float] | None:
    """시설 보정 → 일반 기본표 순으로 base/amplitude를 찾는다."""
    if facility_type:
        facility = FACILITY_PRESETS.get(facility_type.upper())
        if facility is not None:
            override = facility.get(preset, {}).get(name)
            if override is not None:
                return override
    return ENVIRONMENT_PRESETS.get(preset, {}).get(name)


def profile_for(
    name: str, preset: str = DEFAULT_PROFILE, facility_type: str | None = None
) -> Profile:
    """프리셋이 지정한 base/amplitude만 갈아끼운 Profile.

    facility_type을 주면 그 시설의 법정 밴드에 맞춘 보정이 우선 적용된다.
    """
    profile = SENSOR_PROFILES[name]
    override = _override_for(name, preset, facility_type)
    if override is None:
        return profile
    base, amplitude = override
    return replace(profile, base=base, amplitude=amplitude)


def sensor_value(
    name: str,
    ts: datetime,
    seed: int = 0,
    preset: str = DEFAULT_PROFILE,
    facility_type: str | None = None,
) -> float:
    profile = profile_for(name, preset, facility_type)
    hours = ts.hour + ts.minute / 60.0
    phase = 2.0 * math.pi * (hours - profile.peak_hour) / 24.0
    value = profile.base + profile.amplitude * math.cos(phase)
    value += profile.noise * _unit_noise(name, ts, seed)
    value = max(profile.minimum, min(profile.maximum, value))
    return round(value, profile.decimals)


def reading(
    device_type: str,
    ts: datetime,
    seed: int = 0,
    preset: str = DEFAULT_PROFILE,
    facility_type: str | None = None,
) -> dict[str, float]:
    fields = DEVICE_FIELDS.get(device_type, AIR_QUALITY_FIELDS)
    return {
        name: sensor_value(name, ts, seed, preset, facility_type) for name in fields
    }
