import random

from livesim.config import EventSpec
from livesim.scheduler import EventScheduler

INTERVAL = 300
# 하루 틱 수(86400/300 = 288)와 같으면 틱당 확률이 1.0이 되어 반드시 발생한다.
CERTAIN = 288.0


def spec(
    event_type: str = "dropout",
    per_day: float = CERTAIN,
    duration=(5.0, 5.0),
    overrides=None,
) -> EventSpec:
    return EventSpec(event_type, per_day, duration, overrides)


def scheduler(events, seed: int = 1) -> EventScheduler:
    return EventScheduler(events, INTERVAL, rng=random.Random(seed))


def test_certain_event_starts_for_every_device():
    plan = scheduler([spec()]).tick(["a", "b"], now=0.0)

    assert {event.device_id for event in plan.started} == {"a", "b"}
    assert set(plan.active) == {"a", "b"}
    assert plan.ended == ()


def test_event_expires_after_its_duration():
    """만료를 시작보다 먼저 처리하므로, 끝난 디바이스는 같은 틱에 다시 뽑힐 수 있다."""
    sched = scheduler([spec(duration=(5.0, 5.0))])
    sched.tick(["a"], now=0.0)

    plan = sched.tick(["a"], now=300.0)  # 5분 = 300초

    assert [event.device_id for event in plan.ended] == ["a"]
    assert [event.device_id for event in plan.started] == ["a"]


def test_event_survives_until_deadline():
    sched = scheduler([spec(duration=(5.0, 5.0))])
    sched.tick(["a"], now=0.0)

    plan = sched.tick(["a"], now=299.0)

    assert plan.ended == ()
    assert plan.active["a"].type == "dropout"


def test_active_device_does_not_start_a_second_event():
    sched = scheduler([spec(duration=(60.0, 60.0))])
    first = sched.tick(["a"], now=0.0)

    second = sched.tick(["a"], now=10.0)

    assert second.started == ()
    assert second.active["a"].ends_at == first.active["a"].ends_at


def test_only_the_first_matching_event_type_applies():
    """한 디바이스가 dropout과 alert_burst를 동시에 가질 수는 없다."""
    sched = scheduler([spec("dropout"), spec("alert_burst", overrides={"pm25": 100})])

    plan = sched.tick(["a"], now=0.0)

    assert plan.active["a"].type == "dropout"


def test_same_seed_produces_identical_schedule():
    devices = [f"dev-{i}" for i in range(8)]
    events = [spec(per_day=CERTAIN / 2)]  # 틱당 확률 0.5

    def run(seed):
        sched = scheduler(events, seed=seed)
        return [
            tuple(event.device_id for event in sched.tick(devices, now=t * 300.0).started)
            for t in range(5)
        ]

    first, second = run(42), run(42)

    assert first == second
    # 전부 발생하거나 전혀 발생하지 않으면 결정성 검증이 무의미해진다.
    started = {device_id for tick in first for device_id in tick}
    assert 0 < len(started) < len(devices)


def test_different_seeds_produce_different_schedules():
    devices = [f"dev-{i}" for i in range(8)]
    events = [spec(per_day=CERTAIN / 2)]

    def run(seed):
        sched = scheduler(events, seed=seed)
        return [
            tuple(event.device_id for event in sched.tick(devices, now=t * 300.0).started)
            for t in range(5)
        ]

    assert run(1) != run(2)


def test_alert_burst_overrides_carry_bounded_noise():
    sched = scheduler([spec("alert_burst", overrides={"pm25": 100.0, "co2": 2000.0})])

    overrides = sched.tick(["a"], now=0.0).active["a"].overrides

    assert set(overrides) == {"pm25", "co2"}
    assert 90.0 <= overrides["pm25"] <= 110.0
    assert 1800.0 <= overrides["co2"] <= 2200.0
    assert overrides["pm25"] != 100.0  # 노이즈가 실제로 적용됨


def test_alert_burst_values_move_between_ticks():
    sched = scheduler([spec("alert_burst", duration=(60.0, 60.0), overrides={"pm25": 100.0})])

    first = sched.tick(["a"], now=0.0).active["a"].overrides["pm25"]
    second = sched.tick(["a"], now=300.0).active["a"].overrides["pm25"]

    assert first != second


def test_non_alert_events_have_no_overrides():
    plan = scheduler([spec("silence")]).tick(["a"], now=0.0)
    assert plan.active["a"].overrides is None


def test_no_events_means_no_activity():
    plan = EventScheduler([], INTERVAL, rng=random.Random(1)).tick(["a", "b"], now=0.0)

    assert plan.started == ()
    assert plan.ended == ()
    assert plan.active == {}
