"""센서 파형 생성 — 일주기 + 결정적 노이즈. 순수 함수이며 부작용이 없다."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
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


def sensor_value(name: str, ts: datetime, seed: int = 0) -> float:
    profile = SENSOR_PROFILES[name]
    hours = ts.hour + ts.minute / 60.0
    phase = 2.0 * math.pi * (hours - profile.peak_hour) / 24.0
    value = profile.base + profile.amplitude * math.cos(phase)
    value += profile.noise * _unit_noise(name, ts, seed)
    value = max(profile.minimum, min(profile.maximum, value))
    return round(value, profile.decimals)


def reading(device_type: str, ts: datetime, seed: int = 0) -> dict[str, float]:
    fields = DEVICE_FIELDS.get(device_type, AIR_QUALITY_FIELDS)
    return {name: sensor_value(name, ts, seed) for name in fields}
