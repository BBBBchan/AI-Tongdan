"""Small feedback controller for observation work, independent of media capture.

Frame count improves coverage; the time between submissions determines update
cadence. Neither changes the capture rate, player clock or HLS playlist.
"""
from collections import deque
import math


class AdaptiveObservation:
    def __init__(self, initial_frames=4, max_frames=8, target_lag=15.0, concurrency=2):
        self.levels = tuple(n for n in (2, 4, 6, 8, 10) if n <= max_frames)
        if not self.levels:
            raise ValueError("自适应观察至少需要 2 帧。")
        self.frame_count = min(self.levels, key=lambda n: abs(n - initial_frames))
        self.target_lag = max(3.0, float(target_lag))
        self.concurrency = max(1, int(concurrency))
        self.samples = {n: deque(maxlen=20) for n in self.levels}
        self.ema = {}
        self.healthy_completions = 0
        self.reason = "warming_up"
        self.margin = 1.5
        self.submit_interval = 2.0
        self._update_interval()

    def predicted_seconds(self, count=None):
        count = self.frame_count if count is None else count
        values = sorted(self.samples.get(count, ()))
        if values:
            p90 = values[max(0, math.ceil(len(values) * .9) - 1)]
            return max(p90, self.ema[count])
        known = [n for n in self.levels if self.samples[n]]
        if not known:
            return 4.0
        nearest = min(known, key=lambda n: abs(n - count))
        # Unmeasured larger batches need explicit headroom before a trial.
        # Smaller batches are not assumed to remove fixed model overhead.
        return self.predicted_seconds(nearest) * (1 + .12 * max(0, count - nearest))

    def _update_interval(self):
        predicted = self.predicted_seconds()
        sustainable_gap = predicted / self.concurrency
        coverage_gap = self.frame_count / 2
        budget_gap = max(.75, self.target_lag - predicted - self.margin)
        # If the budget is infeasible, a faster producer cannot make the model
        # faster: it bunches concurrent requests, leaving long output gaps.
        # Preserve a sustainable, staggered cadence while reducing batch size.
        self.submit_interval = round(max(.75, sustainable_gap, min(5.0, coverage_gap, budget_gap)), 3)

    def record(self, count, inference_seconds, completion_age, update_interval=None, peak_age=None):
        elapsed = max(.001, float(inference_seconds))
        if count in self.samples:
            self.samples[count].append(elapsed)
            self.ema[count] = .25 * elapsed + .75 * self.ema.get(count, elapsed)
        index = self.levels.index(self.frame_count)
        predicted = self.predicted_seconds()
        predicted_peak = predicted + max(self.submit_interval, predicted / self.concurrency) + self.margin
        measured_peak = max(float(completion_age), float(peak_age or 0))
        # A long pause still matters even when the just-completed frame is fresh.
        gap_risk = float(completion_age) + float(update_interval or 0)
        pressure = max(predicted_peak, measured_peak, gap_risk)
        if pressure >= self.target_lag - .5:
            self.frame_count = self.levels[max(0, index - 1)]
            self.healthy_completions = 0
            self.reason = "reducing_latency"
        elif pressure <= self.target_lag * .75:
            self.healthy_completions += 1
            self.reason = "healthy"
            if self.healthy_completions >= 4 and index + 1 < len(self.levels):
                candidate = self.levels[index + 1]
                estimate = self.predicted_seconds(candidate)
                candidate_gap = max(candidate / 2, estimate / self.concurrency)
                if estimate + candidate_gap + self.margin < self.target_lag - 2:
                    self.frame_count = candidate
                    self.reason = "increasing_coverage"
                    self.healthy_completions = 0
        else:
            self.healthy_completions = 0
            self.reason = "holding_budget"
        self._update_interval()

    def failed(self):
        index = self.levels.index(self.frame_count)
        self.frame_count = self.levels[max(0, index - 1)]
        self.healthy_completions = 0
        self.reason = "recovering_after_error"
        self._update_interval()
