"""24시간 이벤트 엔진.

틱마다 디바이스별로 이벤트를 확률적으로 시작시키고, 만료된 이벤트를 걷어낸다.
한 디바이스는 동시에 하나의 이벤트만 갖는다 — dropout 중에 alert_burst가 겹치면
"단절됐는데 오염 경보가 올라오는" 불가능한 조합이 만들어진다.

난수 생성기를 주입할 수 있어 테스트는 완전히 결정적이다.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Iterable, Sequence

from livesim.config import EventSpec

NOISE_RATIO = 0.1
"""alert_burst 목표값에 얹는 상대 노이즈. 매 틱 다시 뽑아 값이 고정되지 않게 한다."""

SECONDS_PER_MINUTE = 60


@dataclass(frozen=True)
class ActiveEvent:
    """지금 이 디바이스에 걸려 있는 이벤트."""

    type: str
    device_id: str
    ends_at: float
    overrides: dict[str, float] | None = None


@dataclass(frozen=True)
class TickPlan:
    started: tuple[ActiveEvent, ...]
    ended: tuple[ActiveEvent, ...]
    active: dict[str, ActiveEvent]


@dataclass
class _EventState:
    spec: EventSpec
    ends_at: float


class EventScheduler:
    def __init__(
        self,
        events: Sequence[EventSpec],
        interval_seconds: int,
        rng: random.Random | None = None,
    ) -> None:
        self.events = tuple(events)
        self.interval_seconds = interval_seconds
        self.rng = rng if rng is not None else random.Random()
        self.probabilities = tuple(
            spec.start_probability(interval_seconds) for spec in self.events
        )
        self._active: dict[str, _EventState] = {}

    def tick(self, device_ids: Iterable[str], now: float) -> TickPlan:
        """만료 → 시작 순으로 상태를 갱신하고 이번 틱의 계획을 돌려준다.

        만료를 먼저 처리해야 방금 끝난 디바이스가 같은 틱에 새 이벤트를 받을 수
        있다. 반대 순서면 이벤트가 최소 한 틱씩 강제로 벌어진다.
        """
        ended = self._expire(now)
        started_ids = self._start(device_ids, now)
        active = {
            device_id: ActiveEvent(
                type=state.spec.type,
                device_id=device_id,
                ends_at=state.ends_at,
                overrides=self._noisy(state.spec.overrides),
            )
            for device_id, state in self._active.items()
        }
        started = tuple(active[device_id] for device_id in started_ids)
        return TickPlan(started=started, ended=ended, active=active)

    def is_active(self, device_id: str) -> bool:
        return device_id in self._active

    # ---- 내부 -----------------------------------------------------------

    def _expire(self, now: float) -> tuple[ActiveEvent, ...]:
        expired = [
            device_id
            for device_id, state in self._active.items()
            if state.ends_at <= now
        ]
        ended = []
        for device_id in expired:
            state = self._active.pop(device_id)
            ended.append(
                ActiveEvent(
                    type=state.spec.type,
                    device_id=device_id,
                    ends_at=state.ends_at,
                    overrides=state.spec.overrides,
                )
            )
        return tuple(ended)

    def _start(self, device_ids: Iterable[str], now: float) -> list[str]:
        started: list[str] = []
        for device_id in device_ids:
            if device_id in self._active:
                continue
            for spec, probability in zip(self.events, self.probabilities, strict=True):
                if self.rng.random() >= probability:
                    continue
                low, high = spec.duration_minutes
                duration = self.rng.uniform(low, high) * SECONDS_PER_MINUTE
                self._active[device_id] = _EventState(spec, now + duration)
                started.append(device_id)
                break
        return started

    def _noisy(self, overrides: dict[str, float] | None) -> dict[str, float] | None:
        if not overrides:
            return None
        return {
            name: value * (1.0 + self.rng.uniform(-NOISE_RATIO, NOISE_RATIO))
            for name, value in overrides.items()
        }
