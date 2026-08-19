from datetime import datetime

import pytest

from livesim.profiles import (
    ENVIRONMENT_PRESETS,
    PRESET_NAMES,
    SENSOR_PROFILES,
    profile_for,
    reading,
    sensor_value,
)

TS = datetime(2026, 7, 20, 14, 30, 0)


def test_value_is_deterministic():
    assert sensor_value("pm25", TS, seed=7) == sensor_value("pm25", TS, seed=7)


def test_different_seeds_give_different_values():
    assert sensor_value("pm25", TS, seed=1) != sensor_value("pm25", TS, seed=2)


@pytest.mark.parametrize("name", sorted(SENSOR_PROFILES))
def test_values_stay_within_bounds(name):
    profile = SENSOR_PROFILES[name]
    for hour in range(24):
        for seed in range(5):
            value = sensor_value(name, TS.replace(hour=hour), seed=seed)
            assert profile.minimum <= value <= profile.maximum


@pytest.mark.parametrize("name", sorted(SENSOR_PROFILES))
def test_extremes_stay_inside_bounds_without_clamping(name):
    """클램핑에 의존하지 않고도 파형 전체가 범위 안에 들어와야 한다.

    sensor_value가 무조건 클램핑하므로 범위 테스트만으로는 "값이 한계에 눌려
    붙어 있는" 비현실적 파형을 잡을 수 없다. 클램핑 이전의 이론적 극값을
    직접 검사한다.
    """
    profile = SENSOR_PROFILES[name]
    assert profile.base - profile.amplitude - profile.noise >= profile.minimum
    assert profile.base + profile.amplitude + profile.noise <= profile.maximum


def test_diurnal_curve_peaks_near_peak_hour():
    """평균적으로 peak_hour 부근이 12시간 뒤보다 높다."""
    profile = SENSOR_PROFILES["co2"]
    peak = sum(
        sensor_value("co2", TS.replace(hour=profile.peak_hour), seed=s)
        for s in range(20)
    )
    trough_hour = (profile.peak_hour + 12) % 24
    trough = sum(
        sensor_value("co2", TS.replace(hour=trough_hour), seed=s) for s in range(20)
    )
    assert peak > trough


def test_fixed_reading_has_common_and_extended_fields():
    r = reading("FIXED", TS)
    assert "pm25" in r and "co2" in r and "temp" in r
    assert "co" in r and "no2" in r and "o2" in r
    assert "heart_rate" not in r


def test_portable_reading_lacks_fixed_extended_fields():
    r = reading("PORTABLE", TS)
    assert "pm25" in r and "co2" in r and "temp" in r
    assert "co" not in r and "no2" not in r and "o2" not in r


def test_wearable_reading_has_biometric_fields():
    r = reading("WEARABLE", TS)
    assert "heart_rate" in r and "spo2" in r and "skin_temp" in r
    assert "co2" not in r


# ---- 환경 프리셋 -------------------------------------------------------


def test_good_preset_leaves_defaults_untouched():
    """기존 동작 보존 — good은 SENSOR_PROFILES 원본 그대로여야 한다."""
    for name in SENSOR_PROFILES:
        assert profile_for(name, "good") is SENSOR_PROFILES[name]


def test_preset_replaces_only_base_and_amplitude():
    """물리 클램프(min/max)와 파형 특성(noise·peak_hour)은 상속한다."""
    good = SENSOR_PROFILES["pm25"]
    bad = profile_for("pm25", "bad")

    assert (bad.base, bad.amplitude) == ENVIRONMENT_PRESETS["bad"]["pm25"]
    assert bad.minimum == good.minimum and bad.maximum == good.maximum
    assert bad.noise == good.noise and bad.peak_hour == good.peak_hour
    assert bad.decimals == good.decimals


def test_unlisted_metric_inherits_good():
    """차등 테이블 — 프리셋에 없는 지표는 good을 그대로 쓴다."""
    assert profile_for("lux", "very_bad") is SENSOR_PROFILES["lux"]


@pytest.mark.parametrize("preset", PRESET_NAMES)
def test_every_preset_stays_inside_physical_bounds(preset):
    """클램프에 눌려 붙지 않고 파형 전체가 범위 안에 들어와야 한다.

    범위를 넘기면 업로드 DTO 검증(422)에 걸리거나 값이 한계에 붙어버린다.
    """
    for name in SENSOR_PROFILES:
        p = profile_for(name, preset)
        assert p.base - p.amplitude - p.noise >= p.minimum, f"{preset}/{name} 하한"
        assert p.base + p.amplitude + p.noise <= p.maximum, f"{preset}/{name} 상한"


@pytest.mark.parametrize("metric", ["pm25", "pm10", "co2", "tvoc"])
def test_pollution_rises_monotonically_across_presets(metric):
    """FE 등급 배지가 프리셋별로 갈리려면 단조 증가해야 한다."""
    values = [
        sensor_value(metric, TS, seed=3, preset=preset) for preset in PRESET_NAMES
    ]
    assert values == sorted(values), f"{metric}: {values}"
    assert values[0] < values[-1]


def test_biometrics_react_only_from_bad():
    """생체는 미세 반영 — moderate까지는 건드리지 않는다."""
    good_hr = sensor_value("heart_rate", TS, seed=1, preset="good")
    moderate_hr = sensor_value("heart_rate", TS, seed=1, preset="moderate")
    bad_hr = sensor_value("heart_rate", TS, seed=1, preset="bad")
    good_spo2 = sensor_value("spo2", TS, seed=1, preset="good")
    bad_spo2 = sensor_value("spo2", TS, seed=1, preset="bad")

    assert moderate_hr == good_hr
    assert bad_hr > good_hr
    assert bad_spo2 < good_spo2
    assert bad_hr - good_hr < 20      # 과장 금지


def test_reading_applies_the_preset():
    good = reading("FIXED", TS, seed=5, preset="good")
    very_bad = reading("FIXED", TS, seed=5, preset="very_bad")

    assert very_bad["pm25"] > good["pm25"]
    assert very_bad["co"] > good["co"]
    assert set(good) == set(very_bad)   # 필드 구성은 그대로


def test_unknown_preset_falls_back_to_good():
    """방어적 — 알 수 없는 이름이 와도 죽지 않고 기본 파형을 쓴다."""
    assert profile_for("pm25", "nope") is SENSOR_PROFILES["pm25"]


def test_unknown_sensor_name_raises():
    with pytest.raises(KeyError):
        sensor_value("nope", TS)
