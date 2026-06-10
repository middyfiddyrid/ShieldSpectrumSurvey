"""Signal detection over accumulated sweep data.

Input is the host-side accumulation for one segment: per-bin frequency, running
average, max-peak, and hit-count (how many passes the bin exceeded threshold).
Noise floor = median of the average trace. Any bin whose max-peak exceeds
floor + threshold is hot; adjacent hot bins (within merge_gap) merge into one
detected signal with a center frequency, estimated bandwidth, and persistence.

Pure python, no I/O — unit tested with synthetic data.
"""

from dataclasses import dataclass, asdict
from statistics import median
from typing import Optional


@dataclass
class DetectedSignal:
    center_mhz: float
    bw_khz: float
    peak_dbm: float
    avg_dbm: float
    floor_dbm: float
    persistence: float          # fraction of passes the hottest bin was hot
    bin_start: int
    bin_end: int

    def to_dict(self):
        return asdict(self)


def noise_floor(avg_trace: list[float]) -> float:
    return median(avg_trace)


def detect_signals(freqs_mhz: list[float], avg: list[float], maxpeak: list[float],
                   hits: list[int], passes: int, threshold_db: float = 12.0,
                   merge_gap_bins: int = 2,
                   floor_dbm: Optional[float] = None) -> tuple[float, list[DetectedSignal]]:
    """Returns (noise_floor_dbm, signals). passes = number of sweep passes that
    contributed to the hit counters (used to normalize persistence)."""
    n = len(freqs_mhz)
    if n == 0 or n != len(avg) or n != len(maxpeak) or n != len(hits):
        raise ValueError("trace arrays must be equal non-zero length")
    floor = floor_dbm if floor_dbm is not None else noise_floor(avg)
    thresh = floor + threshold_db

    hot = [i for i in range(n) if maxpeak[i] >= thresh]
    if not hot:
        return floor, []

    # merge adjacent hot bins (gaps up to merge_gap_bins bridge a single signal)
    runs: list[tuple[int, int]] = []
    start = prev = hot[0]
    for i in hot[1:]:
        if i - prev <= merge_gap_bins + 1:
            prev = i
        else:
            runs.append((start, prev))
            start = prev = i
    runs.append((start, prev))

    bin_width_mhz = (freqs_mhz[-1] - freqs_mhz[0]) / max(n - 1, 1)
    signals = []
    for a, b in runs:
        seg_peak = max(maxpeak[a:b + 1])
        # power-weighted center across the run, weights = dB above floor
        weights = [max(maxpeak[i] - floor, 0.01) for i in range(a, b + 1)]
        wsum = sum(weights)
        center = sum(freqs_mhz[a + j] * w for j, w in enumerate(weights)) / wsum
        persistence = (max(hits[a:b + 1]) / passes) if passes > 0 else 0.0
        signals.append(DetectedSignal(
            center_mhz=round(center, 4),
            bw_khz=round(max((b - a + 1) * bin_width_mhz * 1000.0, bin_width_mhz * 1000.0), 1),
            peak_dbm=round(seg_peak, 1),
            avg_dbm=round(max(avg[a:b + 1]), 1),
            floor_dbm=round(floor, 1),
            persistence=round(min(persistence, 1.0), 3),
            bin_start=a, bin_end=b))
    return floor, signals


class Accumulator:
    """Host-side running accumulation over raw realtime sweeps for one segment,
    independent of whatever calculator mode the device itself is in."""

    def __init__(self, freqs_mhz: list[float], hit_threshold_db: float = 9.0):
        self.freqs = list(freqs_mhz)
        n = len(self.freqs)
        self.avg = [0.0] * n
        self.maxpeak = [-200.0] * n
        self.hits = [0] * n
        self.passes = 0
        self.hit_threshold_db = hit_threshold_db
        self._floor_estimate: Optional[float] = None

    def add_sweep(self, amplitudes: list[float]):
        n = min(len(amplitudes), len(self.freqs))
        self.passes += 1
        k = self.passes
        if self._floor_estimate is None:
            self._floor_estimate = median(amplitudes[:n])
        thresh = self._floor_estimate + self.hit_threshold_db
        for i in range(n):
            a = amplitudes[i]
            self.avg[i] += (a - self.avg[i]) / k      # running mean
            if a > self.maxpeak[i]:
                self.maxpeak[i] = a
            if a >= thresh:
                self.hits[i] += 1

    def to_dict(self):
        return {"freqs": self.freqs, "avg": [round(x, 2) for x in self.avg],
                "maxpeak": [round(x, 2) for x in self.maxpeak],
                "hits": self.hits, "passes": self.passes}
