from datetime import datetime

import pytest

from livesim.profiles import (
    ENVIRONMENT_PRESETS,
    FACILITY_GRADE_BANDS,
    FACILITY_PRESETS,
    PRESET_NAMES,
    SCHOOL_HUMI_BAND,
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


# ---- 시설별 밴드 보정 --------------------------------------------------------

GOOD_G, MODERATE_G, BAD_G = "좋음", "보통", "나쁨"


def _grade(facility: str, name: str, value: float) -> str:
    """FACILITY_GRADE_BANDS 스냅샷으로 등급을 매긴다 (상한 포함)."""
    good_max, moderate_max = FACILITY_GRADE_BANDS[facility][name]
    if value <= good_max:
        return GOOD_G
    if moderate_max is None or value > moderate_max:
        return BAD_G
    return MODERATE_G


def _worst_case(name: str, preset: str, facility: str) -> tuple[float, float]:
    """노이즈·클램프·반올림까지 최악으로 밀어붙인 값 범위.

    sensor_value가 만들 수 있는 값은 반드시 이 안에 들어온다.
    """
    p = profile_for(name, preset, facility)
    lo = max(p.minimum, p.base - p.amplitude - p.noise)
    hi = min(p.maximum, p.base + p.amplitude + p.noise)
    rounding = 0.5 * 10 ** -p.decimals
    return lo - rounding, hi + rounding


def _waveform(name: str, preset: str, facility: str) -> tuple[float, float]:
    """노이즈를 뺀 일주기 파형의 범위."""
    p = profile_for(name, preset, facility)
    return max(p.minimum, p.base - p.amplitude), min(p.maximum, p.base + p.amplitude)


# 시설 × 프리셋 × 지표 → **최악 케이스에도** 유지되어야 하는 등급.
# moderate는 밴드 폭보다 노이즈가 큰 지표가 있어 파형 기준으로 따로 검증한다.
_STRICT_GUARANTEE = {
    "good": {
        "pm25": GOOD_G, "pm10": GOOD_G, "co2": GOOD_G, "tvoc": GOOD_G,
        "hcho": GOOD_G, "no2": GOOD_G, "radon": GOOD_G, "co": GOOD_G,
    },
    "bad": {
        "pm25": BAD_G, "pm10": BAD_G, "co2": BAD_G, "tvoc": BAD_G,
        "hcho": BAD_G, "radon": BAD_G,
        # 일반표가 건드리지 않는 지표는 좋음에 머문다 — 종합 등급은 최악
        # 지표로 갈리므로 '나쁨' 판정에는 이걸로 충분하다.
        "no2": GOOD_G, "co": GOOD_G,
    },
    "very_bad": {
        "pm25": BAD_G, "pm10": BAD_G, "co2": BAD_G, "tvoc": BAD_G,
        "hcho": BAD_G, "radon": BAD_G, "no2": BAD_G, "co": GOOD_G,
    },
}

GUARANTEES: dict[str, dict[str, dict[str, str]]] = {
    "DAYCARE": _STRICT_GUARANTEE,
    "WELFARE": _STRICT_GUARANTEE,
    "SCHOOL": {
        "good": {**_STRICT_GUARANTEE["good"], "noise": GOOD_G, "o3": GOOD_G},
        "bad": {**_STRICT_GUARANTEE["bad"], "noise": BAD_G, "o3": GOOD_G},
        "very_bad": {**_STRICT_GUARANTEE["very_bad"], "noise": BAD_G, "o3": BAD_G},
    },
    "OFFICE": {
        "good": {"pm10": GOOD_G, "co": GOOD_G},
        "bad": {"pm10": BAD_G, "co": GOOD_G},
        "very_bad": {"pm10": BAD_G, "co": GOOD_G},
    },
    "HOME": {
        "good": {"hcho": GOOD_G, "radon": GOOD_G, "co": GOOD_G},
        "bad": {"hcho": BAD_G, "radon": BAD_G, "co": GOOD_G},
        "very_bad": {"hcho": BAD_G, "radon": BAD_G, "co": GOOD_G},
    },
}

# moderate — 파형(base±amplitude) 기준. 노이즈에 의한 경계 걸침은 설계상 허용.
MODERATE_WAVEFORM: dict[str, dict[str, str]] = {
    "DAYCARE": {
        "pm25": MODERATE_G, "pm10": MODERATE_G, "co2": MODERATE_G,
        "tvoc": MODERATE_G, "hcho": MODERATE_G,
        "no2": GOOD_G, "radon": GOOD_G, "co": GOOD_G,
    },
    "WELFARE": {
        "pm25": MODERATE_G, "pm10": MODERATE_G, "co2": MODERATE_G,
        "tvoc": MODERATE_G, "hcho": MODERATE_G,
        "no2": GOOD_G, "radon": GOOD_G, "co": GOOD_G,
    },
    "SCHOOL": {
        "pm25": MODERATE_G, "pm10": MODERATE_G, "co2": MODERATE_G,
        "tvoc": MODERATE_G, "hcho": MODERATE_G,
        "no2": GOOD_G, "radon": GOOD_G, "co": GOOD_G,
        "noise": GOOD_G, "o3": GOOD_G,
    },
    "OFFICE": {"pm10": MODERATE_G, "co": GOOD_G},
    # 가정 hcho는 2단계(적합/부적합)라 '보통'이 없다 — moderate는 적합에 머문다.
    "HOME": {"hcho": GOOD_G, "radon": GOOD_G, "co": GOOD_G},
}


def _guarantee_cases() -> list[tuple[str, str, str, str]]:
    return [
        (facility, preset, metric, expected)
        for facility, presets in GUARANTEES.items()
        for preset, metrics in presets.items()
        for metric, expected in metrics.items()
    ]


@pytest.mark.parametrize(
    ("facility", "preset", "metric", "expected"),
    _guarantee_cases(),
    ids=lambda v: str(v),
)
def test_preset_holds_its_grade_in_the_worst_case(facility, preset, metric, expected):
    """±진폭 + 노이즈 + 반올림 최악값에도 목표 등급 구간을 벗어나지 않는다."""
    low, high = _worst_case(metric, preset, facility)
    assert _grade(facility, metric, low) == expected, f"하한 {low}"
    assert _grade(facility, metric, high) == expected, f"상한 {high}"


@pytest.mark.parametrize(
    ("facility", "metric", "expected"),
    [
        (facility, metric, expected)
        for facility, metrics in MODERATE_WAVEFORM.items()
        for metric, expected in metrics.items()
    ],
    ids=lambda v: str(v),
)
def test_moderate_waveform_sits_in_its_band(facility, metric, expected):
    low, high = _waveform(metric, "moderate", facility)
    assert _grade(facility, metric, low) == expected, f"하한 {low}"
    assert _grade(facility, metric, high) == expected, f"상한 {high}"


@pytest.mark.parametrize(
    ("facility", "preset", "metric", "expected"),
    _guarantee_cases(),
    ids=lambda v: str(v),
)
def test_sampled_values_match_the_guaranteed_grade(
    facility, preset, metric, expected
):
    """산식만 믿지 않는다 — 24시간 × 여러 시드 실측값의 등급도 같아야 한다."""
    for hour in range(24):
        for minute in (0, 30):
            for seed in range(5):
                ts = TS.replace(hour=hour, minute=minute)
                value = sensor_value(metric, ts, seed, preset, facility)
                assert _grade(facility, metric, value) == expected, (
                    f"{facility}/{preset}/{metric} {hour:02d}:{minute:02d} "
                    f"seed={seed} → {value}"
                )


def test_school_humidity_follows_its_two_sided_band():
    """학교 습도는 양방향 밴드 — 낮아도 높아도 나쁨이다."""
    good_low, good_high, _, bad_over = SCHOOL_HUMI_BAND

    low, high = _worst_case("humi", "good", "SCHOOL")
    assert good_low <= low and high <= good_high

    wave_low, wave_high = _waveform("humi", "moderate", "SCHOOL")
    assert good_high < wave_low and wave_high <= bad_over

    for preset in ("bad", "very_bad"):
        low, _ = _worst_case("humi", preset, "SCHOOL")
        assert low > bad_over, f"{preset} 하한 {low}"


def test_co_can_never_reach_bad_by_physics():
    """co는 물리 상한(10ppm)이 '나쁨' 경계와 같아 어떤 프리셋도 넘길 수 없다.

    결함이 아니라 측정기 사양이다. 문서(README §4-B)에도 같은 이유를 적어둔다.
    """
    moderate_max = FACILITY_GRADE_BANDS["DAYCARE"]["co"][1]
    assert SENSOR_PROFILES["co"].maximum <= moderate_max


@pytest.mark.parametrize("facility", sorted(FACILITY_PRESETS))
@pytest.mark.parametrize("preset", PRESET_NAMES)
def test_facility_presets_stay_inside_physical_bounds(facility, preset):
    """시설 보정도 클램프에 눌리지 않아야 한다 (업로드 422·파형 왜곡 방지)."""
    for name in SENSOR_PROFILES:
        p = profile_for(name, preset, facility)
        assert p.base - p.amplitude - p.noise >= p.minimum, f"{facility}/{preset}/{name}"
        assert p.base + p.amplitude + p.noise <= p.maximum, f"{facility}/{preset}/{name}"


def test_facility_correction_wins_over_the_general_table():
    general = profile_for("tvoc", "good")
    daycare = profile_for("tvoc", "good", "DAYCARE")
    assert (general.base, general.amplitude) == (150.0, 80.0)   # 일반표 그대로
    assert daycare.base < general.base


def test_metric_without_a_band_keeps_the_general_value():
    """사무실은 pm25 밴드가 없다 — 일반 기준 값을 그대로 써야 한다."""
    for preset in PRESET_NAMES:
        for metric in ("pm25", "co2", "tvoc"):
            assert profile_for(metric, preset, "OFFICE") == profile_for(metric, preset)


def test_facility_without_bands_falls_back_to_the_general_table():
    """HOME_ELDERLY는 백엔드 시드에 밴드가 없다. 오타·미지원도 같은 경로."""
    for facility in ("HOME_ELDERLY", "NOPE"):
        for preset in PRESET_NAMES:
            for metric in ("pm25", "tvoc", "hcho"):
                assert profile_for(metric, preset, facility) == profile_for(
                    metric, preset
                )


def test_facility_lookup_is_case_insensitive():
    assert profile_for("tvoc", "good", "daycare") == profile_for(
        "tvoc", "good", "DAYCARE"
    )


def test_school_inherits_strict_corrections_and_adds_its_own():
    assert profile_for("pm25", "good", "SCHOOL") == profile_for(
        "pm25", "good", "DAYCARE"
    )
    school_noise = profile_for("noise", "good", "SCHOOL")
    assert school_noise != SENSOR_PROFILES["noise"]
    # 소음 밴드는 학교에만 있다 — 어린이집은 일반 파형 그대로.
    assert profile_for("noise", "good", "DAYCARE") is SENSOR_PROFILES["noise"]


def test_reading_passes_the_facility_through():
    daycare = reading("FIXED", TS, seed=5, preset="good", facility_type="DAYCARE")
    office = reading("FIXED", TS, seed=5, preset="good", facility_type="OFFICE")
    assert daycare["tvoc"] < office["tvoc"]
    assert daycare["pm10"] == office["pm10"]   # 둘 다 일반값(각자 밴드 안)


def test_unknown_preset_with_facility_still_falls_back_to_good():
    assert profile_for("pm25", "nope", "DAYCARE") is SENSOR_PROFILES["pm25"]
