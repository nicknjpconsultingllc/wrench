"""Kaplan-Meier time-to-recovery over right-censored ``(ticks, recovered)`` pairs.

``scoring.time_to_recovery_parts`` yields one datum per fire: the ticks
until sustained recovery, or the budget with ``recovered=False`` when the
run never got there (right-censored: the true time is at least the budget).
Averaging those ticks, or dropping the censored ones, biases toward models
that only attempt easy recoveries. The product-limit estimator here uses
both: a censored fire stays in the at-risk set until its budget and then
leaves without an event.

Convention on ties: at a time ``t`` the at-risk count is every fire with
duration ``>= t``, so a fire censored at exactly ``t`` is still at risk for
the events at ``t`` (events before censorings at equal times).
"""

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

Datum = Tuple[float, bool]  # (ticks, recovered)


@dataclass(frozen=True)
class SurvivalCurve:
    """The step function S(t) = P(not yet recovered by t).

    ``times`` are the distinct event (recovery) times in ascending order;
    ``survival[i]`` is S(t) for ``times[i] <= t < times[i+1]``. S(t) = 1 for
    ``t < times[0]``. ``at_risk[i]`` and ``events[i]`` are the n_i and d_i
    behind each step. ``n`` is the total number of fires, ``censored`` how
    many never recovered.
    """

    times: Tuple[float, ...]
    survival: Tuple[float, ...]
    at_risk: Tuple[int, ...]
    events: Tuple[int, ...]
    n: int
    censored: int

    def survival_at(self, t: float) -> float:
        s = 1.0
        for time, value in zip(self.times, self.survival):
            if time > t:
                break
            s = value
        return s

    @property
    def median(self) -> Optional[float]:
        """Smallest t with S(t) <= 0.5; None when the curve never gets there
        (more than half the fires are censored before any such time)."""
        for time, value in zip(self.times, self.survival):
            if value <= 0.5:
                return time
        return None

    def restricted_mean(self, tau: float) -> float:
        """Restricted mean time-to-recovery: the area under S(t) on
        ``[0, tau]``. Always defined, unlike the median; ``tau`` is
        normally the (common) budget."""
        area = 0.0
        prev_t = 0.0
        prev_s = 1.0
        for time, value in zip(self.times, self.survival):
            if time >= tau:
                break
            area += prev_s * (time - prev_t)
            prev_t, prev_s = time, value
        area += prev_s * (tau - prev_t)
        return area

    def points(self) -> List[Tuple[float, float]]:
        """``(t, S(t))`` pairs including the ``(0, 1)`` origin, for plotting."""
        return [(0.0, 1.0)] + list(zip(self.times, self.survival))


def kaplan_meier(data: Iterable[Datum]) -> SurvivalCurve:
    """Product-limit estimate over ``(ticks, recovered)`` pairs."""
    pairs = [(float(t), bool(r)) for t, r in data]
    event_times = sorted({t for t, r in pairs if r})
    times: List[float] = []
    survival: List[float] = []
    at_risk: List[int] = []
    events: List[int] = []
    s = 1.0
    for t in event_times:
        n = sum(1 for d, _ in pairs if d >= t)
        d = sum(1 for dur, r in pairs if r and dur == t)
        s *= 1.0 - d / n
        times.append(t)
        survival.append(s)
        at_risk.append(n)
        events.append(d)
    return SurvivalCurve(
        times=tuple(times),
        survival=tuple(survival),
        at_risk=tuple(at_risk),
        events=tuple(events),
        n=len(pairs),
        censored=sum(1 for _, r in pairs if not r),
    )


def recovery_data(episodes: Iterable[dict]) -> List[Datum]:
    """Pool the per-fire survival data from ``episode_metrics`` outputs.

    Reads ``episode["time_to_recovery"]["fires"][*]["parts"]``; fires whose
    ``parts`` is None (degenerate baseline, not scoreable) are skipped, and
    censored fires are kept. Feed the result to ``kaplan_meier``.
    """
    data: List[Datum] = []
    for episode in episodes:
        for fire in episode["time_to_recovery"]["fires"]:
            parts = fire["parts"]
            if parts is None:
                continue
            data.append((parts["ticks"], parts["recovered"]))
    return data


def pooled_time_to_recovery(
    episodes: Sequence[dict], tau: Optional[float] = None
) -> Optional[dict]:
    """Cross-episode time-to-recovery summary: the KM curve, its median and
    the restricted mean at ``tau`` (default: the largest budget seen). None
    when no fire was scoreable."""
    data = recovery_data(episodes)
    if not data:
        return None
    curve = kaplan_meier(data)
    if tau is None:
        tau = max(
            fire["parts"]["budget_ticks"]
            for episode in episodes
            for fire in episode["time_to_recovery"]["fires"]
            if fire["parts"] is not None
        )
    return {
        "curve": curve,
        "median_ticks": curve.median,
        "restricted_mean_ticks": curve.restricted_mean(tau),
        "tau": tau,
        "n": curve.n,
        "censored": curve.censored,
    }
