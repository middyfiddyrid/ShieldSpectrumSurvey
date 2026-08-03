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
    # "detection" = cleared the full threshold, reportable as an instrument
    # finding. "candidate" = only cleared the near-floor threshold: visible on
    # the display, worth a look, NOT a finding. Field report that forced this:
    # the operator kept marking by eye signals sitting 0-2 dB over the floor
    # while the detector demanded 12 dB, so the sweep silently dropped exactly
    # the low-power emitters a bug sweep exists to find.
    tier: str = "detection"

    def to_dict(self):
        return asdict(self)


def noise_floor(avg_trace: list[float]) -> float:
    return median(avg_trace)


def detect_signals(freqs_mhz: list[float], avg: list[float], maxpeak: list[float],
                   hits: list[int], passes: int, threshold_db: float = 12.0,
                   merge_gap_bins: int = 2,
                   floor_dbm: Optional[float] = None,
                   candidate_threshold_db: Optional[float] = None,
                   low_hits: Optional[list[int]] = None,
                   persistence_min_frac: float = 0.85,
                   persistence_min_passes: int = 8
                   ) -> tuple[float, list[DetectedSignal]]:
    """Returns (noise_floor_dbm, signals). passes = number of sweep passes that
    contributed to the hit counters (used to normalize persistence).

    candidate_threshold_db (< threshold_db) turns on the NEAR-FLOOR tier: bins
    that clear it but not threshold_db come back tagged tier="candidate". They
    are leads for review, never findings — see DetectedSignal.tier. None keeps
    the single-tier behaviour exactly as before.

    low_hits (from Accumulator.low_hits) additionally enables PERSISTENCE
    detection. Amplitude alone cannot find a carrier sitting AT the floor — the
    operator was marking signals 0-2 dB over it by eye and no threshold could
    have caught them without also admitting noise. Consistency separates them:
    a bin only fractionally above the floor on >= persistence_min_frac of passes
    is an emitter, because noise does not favour one bin. These also come back
    as tier="candidate" — a lead, never a finding."""
    n = len(freqs_mhz)
    if n == 0 or n != len(avg) or n != len(maxpeak) or n != len(hits):
        raise ValueError("trace arrays must be equal non-zero length")
    floor = floor_dbm if floor_dbm is not None else noise_floor(avg)
    thresh = floor + threshold_db
    # A candidate threshold at or above the detection threshold would silently
    # relabel real detections as leads — refuse it rather than weaken the report.
    cand_thresh = None
    if candidate_threshold_db is not None and candidate_threshold_db < threshold_db:
        cand_thresh = floor + candidate_threshold_db

    scan_thresh = cand_thresh if cand_thresh is not None else thresh
    hot = set(i for i in range(n) if maxpeak[i] >= scan_thresh)
    # persistence tier — only when the caller supplied low-threshold hit counts
    # AND the candidate tier is on, so single-tier callers are untouched
    if (low_hits is not None and cand_thresh is not None
            and passes >= persistence_min_passes and len(low_hits) == n):
        hot |= set(i for i in range(n)
                   if low_hits[i] / passes >= persistence_min_frac)
    hot = sorted(hot)
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
            bin_start=a, bin_end=b,
            # the RUN's own peak decides its tier — a run that never reaches the
            # full threshold is a lead, however wide it is
            tier="detection" if seg_peak >= thresh else "candidate"))
    return floor, signals


class Accumulator:
    """Host-side running accumulation over raw realtime sweeps for one segment,
    independent of whatever calculator mode the device itself is in."""

    def __init__(self, freqs_mhz: list[float], hit_threshold_db: float = 9.0,
                 low_hit_threshold_db: float = 0.5):
        self.freqs = list(freqs_mhz)
        n = len(self.freqs)
        self.avg = [0.0] * n
        self.maxpeak = [-200.0] * n
        self.hits = [0] * n
        # A SECOND hit counter at a much lower threshold. Amplitude alone cannot
        # find a weak carrier sitting AT the noise floor — but consistency can:
        # thermal noise crossing floor+2 dB lands in a different bin every pass,
        # while a real emitter does it in the SAME bin every pass. Counting that
        # separately is what lets the detector see what the operator's eye sees
        # on an accumulating max-hold display.
        self.low_hits = [0] * n
        self.passes = 0
        self.hit_threshold_db = hit_threshold_db
        self.low_hit_threshold_db = low_hit_threshold_db
        self._floor_estimate: Optional[float] = None

    def add_sweep(self, amplitudes: list[float]):
        n = min(len(amplitudes), len(self.freqs))
        self.passes += 1
        k = self.passes
        if self._floor_estimate is None:
            self._floor_estimate = median(amplitudes[:n])
        thresh = self._floor_estimate + self.hit_threshold_db
        low_thresh = self._floor_estimate + self.low_hit_threshold_db
        for i in range(n):
            a = amplitudes[i]
            self.avg[i] += (a - self.avg[i]) / k      # running mean
            if a > self.maxpeak[i]:
                self.maxpeak[i] = a
            if a >= thresh:
                self.hits[i] += 1
            if a >= low_thresh:
                self.low_hits[i] += 1

    def to_dict(self):
        return {"freqs": self.freqs, "avg": [round(x, 2) for x in self.avg],
                "maxpeak": [round(x, 2) for x in self.maxpeak],
                "hits": self.hits, "low_hits": self.low_hits,
                "passes": self.passes}
