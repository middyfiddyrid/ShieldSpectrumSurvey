"""Sweep segment planning.

Splits each hardware-reported module range into sweep windows (RF Explorer
returns ~112 points per pass, so narrower span = better RBW), then fits the
operator's time budget: every segment gets AVERAGE passes plus MaxHold dwell,
with dwell allocated disproportionately to the bands where surveillance devices
live (risk weights from config sweep.band_dwell_weights keyed by allocation
band tags).

Pure python — unit tested in tests/test_planner.py.
"""

from dataclasses import dataclass, field, asdict

from ..allocations import band_tag_for_range

SEGMENT_OVERHEAD_S = 0.9      # range reconfiguration + settle per segment
MIN_AVG_PASSES = 3
MIN_MAXHOLD_DWELL_S = 2.0
AVG_TIME_SHARE = 0.6          # of each segment's budget: 60% AVG passes, 40% MaxHold


@dataclass
class Segment:
    module: str               # 'left' | 'right'
    low_mhz: float
    high_mhz: float
    band_tag: str
    weight: float
    avg_passes: int = 0
    maxhold_dwell_s: float = 0.0

    @property
    def span_mhz(self):
        return self.high_mhz - self.low_mhz

    def rbw_khz(self, sweep_points: int) -> float:
        return self.span_mhz * 1000.0 / sweep_points

    def to_dict(self):
        d = asdict(self)
        d["span_mhz"] = round(self.span_mhz, 3)
        return d


@dataclass
class SweepPlan:
    segments: list = field(default_factory=list)
    profile: str = "standard"
    estimated_seconds: float = 0.0
    sweep_points: int = 112

    def to_dict(self):
        return {"segments": [s.to_dict() for s in self.segments],
                "profile": self.profile,
                "estimated_seconds": round(self.estimated_seconds, 0),
                "estimated_minutes": round(self.estimated_seconds / 60.0, 1),
                "segment_count": len(self.segments),
                "sweep_points": self.sweep_points}


def _segment_width_mhz(center_mhz: float, widths: dict, span_scale: float) -> float:
    if center_mhz < 1000.0:
        base = widths.get("subghz", 75)
    elif center_mhz < 2400.0:
        base = widths.get("mid", 100)
    else:
        base = widths.get("high", 200)
    return max(2.0, base * span_scale)


def split_range(module: str, lo_mhz: float, hi_mhz: float, widths: dict,
                span_scale: float, weights: dict) -> list[Segment]:
    segments = []
    cur = lo_mhz
    while cur < hi_mhz - 1e-6:
        width = _segment_width_mhz(cur, widths, span_scale)
        end = min(cur + width, hi_mhz)
        tag = band_tag_for_range(cur, end)
        segments.append(Segment(module, round(cur, 4), round(end, 4), tag,
                                weights.get(tag, weights.get("default", 1.0))))
        cur = end
    return segments


def plan_module_sweep(module: str, lo_mhz: float, hi_mhz: float, cfg,
                      profile_name: str, custom_minutes: float = 0.0) -> SweepPlan:
    """Build a plan for one module fitting the selected time budget."""
    widths = cfg.get("sweep", "segment_width_mhz", default={})
    weights = cfg.get("sweep", "band_dwell_weights", default={"default": 1.0})
    profiles = cfg.get("sweep", "profiles", default={})
    sps = float(cfg.get("sweep", "seconds_per_sweep", default=0.35))
    points = int(cfg.get("sweep", "sweep_points", default=112))

    if profile_name == "custom":
        minutes = max(3.0, custom_minutes)
        span_scale, base_passes = 1.0, 10
    else:
        prof = profiles.get(profile_name, profiles.get("standard", {}))
        minutes = float(prof.get("minutes", 45))
        span_scale = float(prof.get("span_scale", 1.0))
        base_passes = int(prof.get("avg_passes", 10))

    # Full sweep covers BOTH modules within one budget; each module call gets
    # a share proportional to its span. The caller passes module_share.
    segments = split_range(module, lo_mhz, hi_mhz, widths, span_scale, weights)
    if not segments:
        return SweepPlan([], profile_name, 0.0, points)

    budget_s = minutes * 60.0
    overhead = len(segments) * SEGMENT_OVERHEAD_S
    usable = max(budget_s - overhead,
                 len(segments) * (MIN_AVG_PASSES * sps + MIN_MAXHOLD_DWELL_S))

    total_weight = sum(s.weight for s in segments)
    for s in segments:
        t_seg = usable * (s.weight / total_weight)
        s.avg_passes = max(MIN_AVG_PASSES,
                           min(int(t_seg * AVG_TIME_SHARE / sps), base_passes * 4))
        s.maxhold_dwell_s = round(max(MIN_MAXHOLD_DWELL_S, t_seg * (1 - AVG_TIME_SHARE)), 1)

    est = overhead + sum(s.avg_passes * sps + s.maxhold_dwell_s for s in segments)
    return SweepPlan(segments, profile_name, est, points)


def plan_full_sweep(modules: list[dict], cfg, profile_name: str,
                    custom_minutes: float = 0.0) -> dict[str, SweepPlan]:
    """modules: [{'side','min_mhz','max_mhz'}, ...] exactly as the hardware
    reported them. Splits the time budget across modules by span share."""
    profiles = cfg.get("sweep", "profiles", default={})
    if profile_name == "custom":
        total_minutes = max(3.0, custom_minutes)
    else:
        total_minutes = float(profiles.get(profile_name, profiles.get("standard", {}))
                              .get("minutes", 45))
    spans = {m["side"]: max(m["max_mhz"] - m["min_mhz"], 1.0) for m in modules}
    total_span = sum(spans.values())
    plans = {}
    for m in modules:
        share = spans[m["side"]] / total_span
        plans[m["side"]] = plan_module_sweep(
            m["side"], m["min_mhz"], m["max_mhz"], cfg,
            "custom", total_minutes * share) if profile_name == "custom" else \
            _plan_with_minutes(m, cfg, profile_name, total_minutes * share)
    return plans


def _plan_with_minutes(m: dict, cfg, profile_name: str, minutes: float) -> SweepPlan:
    profiles = cfg.get("sweep", "profiles", default={})
    prof = dict(profiles.get(profile_name, profiles.get("standard", {})))
    prof["minutes"] = minutes
    # temporarily run plan_module_sweep against an adjusted profile
    class _ProxyCfg:
        def __init__(self, real, prof, name):
            self._real, self._prof, self._name = real, prof, name

        def get(self, *keys, default=None):
            if keys[:2] == ("sweep", "profiles"):
                merged = dict(self._real.get("sweep", "profiles", default={}))
                merged[self._name] = self._prof
                return merged
            return self._real.get(*keys, default=default)

    plan = plan_module_sweep(m["side"], m["min_mhz"], m["max_mhz"],
                             _ProxyCfg(cfg, prof, profile_name), profile_name)
    return plan
