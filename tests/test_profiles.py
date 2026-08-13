from datetime import datetime

import pytest

from livesim.profiles import SENSOR_PROFILES, reading, sensor_value

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


def test_unknown_sensor_name_raises():
    with pytest.raises(KeyError):
        sensor_value("nope", TS)
