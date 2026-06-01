# src/fusion.py
"""
Stress fusion engine — aligned with the VRET Biofeedback Pipeline technical
report (PDF §2, §4, §7).

Turns the per-tick (EDA phasic, HR BPM, HRV milliseconds) inputs into the
canonical stress index S_t plus a categorical state (calm / stressed /
ultra_stressed) and a 0-100 operator score. The thresholds are derived once
from the resting baseline and stay frozen for the session.

What is different from the original modular pipeline (and why):

  * EDA is the PHASIC component, decomposed with nk.eda_phasic — not raw
    EDA, which was ~99% slow tonic drift on the reference recording and
    dominated S_t at weight 0.5 (PDF §7 Cause 1).

  * Each signal is Z-SCORED against its own baseline mean + sigma before
    the 0.5 / 0.3 / 0.2 weights are applied, so the weights express
    relative importance rather than an accident of which signal has larger
    raw numbers (PDF §7 Cause 2).

  * Thresholds are centred on the resting S_t mean — `thresh = mean_baseline
    + K * sigma_baseline` — not measured from zero, which would flag a calm
    participant whose resting S_t naturally drifts positive (PDF §4 Bug 6).

  * sigma_baseline comes from the RAW instantaneous S_inst series, not the
    smoothed S_t. Averaging shrinks variance ~√N times, which collapsed the
    "stressed" band to <1 unit and made every real arousal leap past
    "stressed" straight into "ultra-stressed" (PDF §4 Bug 7).

  * THRESH_MILD_K / HIGH_K are 1.28 / 2.33 — the true 90th- and 99th-
    percentile z-scores from the normal distribution.

This module does not compute a balloon altitude. Unity owns altitude
control: we expose the state, Unity converts that into "increase / hold /
decrease" balloon movement on its side. Keeping the altitude math out of
Python means the same stress signal can drive any other VR exposure
paradigm without changes here.
"""

import collections
import numpy as np
from config import Config


class FusionEngine:
    """Stress index, classification, and 0-100 display score."""

    # Fallback sigma when baseline computation degenerates (e.g. a sensor was
    # flat for the whole 120 s). Keeps the system in a usable state instead
    # of locking thresholds at 0, which would label every tick ultra-stressed.
    SIGMA_FALLBACK = 1.5

    def __init__(self):
        # 50-sample rolling buffer to smooth S_instant into S_t (1 second at 50 Hz)
        self.s_instant_buffer = collections.deque(maxlen=int(Config.PIPELINE_RATE))

        # Frozen at the end of the 120 s baseline; zero until then.
        self.thresh_mild = 0.0
        self.thresh_high = 0.0
        # Resting S_t mean — the *centre* of the calm/stressed/ultra bands.
        # Without this, thresholds were placed against zero (PDF Bug 6).
        self.mean_baseline = 0.0

        # Per-signal baseline stats for z-scoring (PDF Cause 2). Filled in
        # by calculate_baseline_sigma at end of BASELINE; zeros until then.
        self.eda_phasic_mean = 0.0
        self.eda_phasic_sigma = Config.SIGMA_FLOOR
        self.hr_pct_mean = 0.0
        self.hr_pct_sigma = Config.SIGMA_FLOOR
        self.hrv_pct_mean = 0.0
        self.hrv_pct_sigma = Config.SIGMA_FLOOR

    def reset(self):
        """Discard thresholds and the rolling buffer. Used on operator reset."""
        self.s_instant_buffer.clear()
        self.thresh_mild = 0.0
        self.thresh_high = 0.0
        self.mean_baseline = 0.0
        self.eda_phasic_mean = 0.0
        self.eda_phasic_sigma = Config.SIGMA_FLOOR
        self.hr_pct_mean = 0.0
        self.hr_pct_sigma = Config.SIGMA_FLOOR
        self.hrv_pct_mean = 0.0
        self.hrv_pct_sigma = Config.SIGMA_FLOOR
        print("[FUSION] Reset: thresholds + per-signal stats cleared, buffer emptied.")

    def set_thresholds(self, mean_baseline: float, sigma_baseline: float):
        """
        Lock the session's two thresholds at:
            thresh = mean_baseline + K * sigma_baseline
        per PDF §2 (Bug 6 fix). The K constants are the z-scores for the
        90th and 99th percentiles of the normal distribution (1.28 / 2.33).

        sigma_baseline is the standard deviation of the RAW instantaneous
        S_inst series (PDF §4 Bug 7 fix — using the smoothed S_t std would
        collapse the bands).
        """
        if sigma_baseline is None or sigma_baseline <= 1e-6:
            print(f"[FUSION] WARN: sigma_baseline={sigma_baseline} is degenerate. "
                  f"Using fallback sigma={self.SIGMA_FALLBACK}.")
            sigma_baseline = self.SIGMA_FALLBACK

        self.mean_baseline = float(mean_baseline)
        self.thresh_mild = self.mean_baseline + Config.THRESH_MILD_K * sigma_baseline
        self.thresh_high = self.mean_baseline + Config.THRESH_HIGH_K * sigma_baseline
        print(f"[FUSION] Thresholds locked -> mean_baseline: {self.mean_baseline:+.3f} | "
              f"mild: {self.thresh_mild:+.3f} | high: {self.thresh_high:+.3f} "
              f"(sigma_baseline={sigma_baseline:.4f})")

    def calculate_baseline_sigma(self, cleaned_buffers: dict,
                                  personal_averages: dict,
                                  phasic_buffer=None) -> tuple:
        """
        Per the procedural reference (vret_server_v2.py), this is now a
        two-pass computation:

          Pass 1 — for each baseline sample, build raw per-signal "deltas":
                     - phasic EDA in uS (already tonic-removed)
                     - HR percent deviation from personal baseline
                     - HRV percent deviation from personal baseline (inverted)
                   Compute each signal's own mean + sigma. These are stored on
                   the engine and used to z-score live values in compute_s_instant.

          Pass 2 — z-score each per-sample delta against its own baseline,
                   apply the 0.5/0.3/0.2 weights, accumulate the resulting raw
                   S_inst series, smooth to S_t with the same 1 s rolling mean
                   used live. mean_baseline = mean(smoothed S_t), sigma_baseline
                   = std(raw S_inst).

        Returns:
            (mean_baseline, sigma_baseline)  — caller passes both to
            set_thresholds().
        """
        eda_arr = cleaned_buffers.get('eda')
        hr_arr = cleaned_buffers.get('hr')
        hrv_arr = cleaned_buffers.get('hrv')

        if eda_arr is None or hr_arr is None or hrv_arr is None:
            print("[FUSION] WARN: cleaned buffers missing; falling back to sigma=1.5")
            return 0.0, self.SIGMA_FALLBACK

        n = min(len(eda_arr), len(hr_arr), len(hrv_arr))
        if n < int(Config.PIPELINE_RATE):
            print("[FUSION] WARN: cleaned buffers too small; falling back to sigma=1.5")
            return 0.0, self.SIGMA_FALLBACK

        avg_hr = float(personal_averages.get('hr') or 1e-6)
        avg_hrv = float(personal_averages.get('hrv') or 1e-6)

        # Phasic baseline buffer: per-tick phasic EDA values captured during
        # BASELINE. If absent (e.g. NeuroKit unavailable), fall back to zeros
        # — z-scoring is then well-defined but the EDA contribution is just
        # noise around 0.
        if phasic_buffer is None or len(phasic_buffer) == 0:
            print("[FUSION] WARN: phasic EDA baseline missing; EDA term will be ~0.")
            phasic_arr = np.zeros(n, dtype=np.float64)
        else:
            phasic_arr = np.asarray(phasic_buffer, dtype=np.float64)
            # Length-align to the shortest cleaned buffer.
            if len(phasic_arr) >= n:
                phasic_arr = phasic_arr[:n]
            else:
                pad = np.zeros(n - len(phasic_arr), dtype=np.float64)
                phasic_arr = np.concatenate([phasic_arr, pad])

        # ---- Pass 1: raw per-signal deltas + per-signal stats ----
        hr_pct = (np.asarray(hr_arr[:n], dtype=np.float64) - avg_hr) / avg_hr * 100.0
        hrv_pct = (avg_hrv - np.asarray(hrv_arr[:n], dtype=np.float64)) / avg_hrv * 100.0

        self.eda_phasic_mean = float(np.mean(phasic_arr))
        self.eda_phasic_sigma = max(float(np.std(phasic_arr)), Config.SIGMA_FLOOR)
        self.hr_pct_mean = float(np.mean(hr_pct))
        self.hr_pct_sigma = max(float(np.std(hr_pct)), Config.SIGMA_FLOOR)
        self.hrv_pct_mean = float(np.mean(hrv_pct))
        self.hrv_pct_sigma = max(float(np.std(hrv_pct)), Config.SIGMA_FLOOR)

        print(f"[FUSION] Per-signal baselines (for z-scoring):")
        print(f"         EDA phasic: mean={self.eda_phasic_mean:+.4f} uS, "
              f"sigma={self.eda_phasic_sigma:.4f} uS")
        print(f"         HR  delta : mean={self.hr_pct_mean:+.2f}%, "
              f"sigma={self.hr_pct_sigma:.2f}%")
        print(f"         HRV delta : mean={self.hrv_pct_mean:+.2f}%, "
              f"sigma={self.hrv_pct_sigma:.2f}%")

        # ---- Pass 2: z-scored S_inst series + smoothed S_t series ----
        z_eda = (phasic_arr - self.eda_phasic_mean) / self.eda_phasic_sigma
        z_hr = (hr_pct - self.hr_pct_mean) / self.hr_pct_sigma
        z_hrv = (hrv_pct - self.hrv_pct_mean) / self.hrv_pct_sigma
        s_instant_series = (Config.WEIGHT_EDA * z_eda
                            + Config.WEIGHT_HRV * z_hrv
                            + Config.WEIGHT_HR * z_hr)

        window = int(Config.PIPELINE_RATE)
        kernel = np.ones(window, dtype=np.float64) / window
        s_t_series = np.convolve(s_instant_series, kernel, mode='valid')

        # PDF Bug 7: sigma from RAW s_inst, mean from smoothed s_t (the
        # centring matches what live classification operates on).
        sigma_raw = float(np.std(s_instant_series))
        sigma_smoothed = float(np.std(s_t_series))
        mean_baseline = float(np.mean(s_t_series))
        print(f"[FUSION] Baseline S_t: mean={mean_baseline:+.4f}, "
              f"sigma_raw={sigma_raw:.4f} (used), "
              f"sigma_smoothed={sigma_smoothed:.4f} (reference only)")
        return mean_baseline, sigma_raw

    def compute_deltas(self, live_vector: list, phasic_eda: float,
                       baseline_averages: dict) -> dict:
        """
        Per-signal display values for the LSL stream and the live transcript.
        After the PDF math fix, the EDA quantity is phasic EDA in microsiemens
        (tonic-removed, what drives the score). HR and HRV remain percent
        deviations from baseline.

        Note: channel 6 of the LSL stream (`delta_eda`) now carries phasic
        EDA in µS, not a percent. Documented in OUTPUTS.md.
        """
        eda, hr, hrv = live_vector
        base_hr = baseline_averages['hr'] or 1e-6
        base_hrv = baseline_averages['hrv'] or 1e-6
        return {
            'eda': float(phasic_eda),  # phasic uS, not percent
            'hr':  ((hr - base_hr) / base_hr) * 100.0,
            'hrv': ((base_hrv - hrv) / base_hrv) * 100.0,
        }

    def compute_s_instant(self, live_vector: list, phasic_eda: float,
                           baseline_averages: dict) -> float:
        """
        Z-scored, weighted composite stress score (PDF §2):

            S_inst = 0.5 * z(phasic_EDA) + 0.3 * z(HRV%) + 0.2 * z(HR%)

        Each signal is in units of its own baseline spread before the weights
        are applied, so EDA's larger raw magnitude no longer dominates by
        scale alone.
        """
        d = self.compute_deltas(live_vector, phasic_eda, baseline_averages)
        z_eda = (d['eda'] - self.eda_phasic_mean) / self.eda_phasic_sigma
        z_hr = (d['hr']  - self.hr_pct_mean)     / self.hr_pct_sigma
        z_hrv = (d['hrv'] - self.hrv_pct_mean)    / self.hrv_pct_sigma
        return (Config.WEIGHT_EDA * z_eda
                + Config.WEIGHT_HRV * z_hrv
                + Config.WEIGHT_HR * z_hr)

    def evaluate_state(self, s_instant: float) -> tuple:
        """
        Steps 7 + 9 + 10: apply the 1-second rolling mean to get S_t,
        classify into one of three states, compute the 0-100 display score.

        With the PDF Bug 6 fix, S_t is compared against thresholds CENTRED
        on mean_baseline, not against zero. The 0-100 display score is
        likewise mapped from mean_baseline (0) to thresh_high (100).

        Returns:
            (S_t, state_label, operator_dashboard_score)
        """
        self.s_instant_buffer.append(s_instant)

        # Wait until the buffer has 1 full second of data before evaluating
        if len(self.s_instant_buffer) < int(Config.PIPELINE_RATE):
            return 0.0, "calm", 0.0

        s_t = sum(self.s_instant_buffer) / len(self.s_instant_buffer)

        # State classification — bands are centred on mean_baseline.
        if s_t > self.thresh_high:
            state = "ultra_stressed"
        elif s_t > self.thresh_mild:
            state = "stressed"
        else:
            state = "calm"

        # 0-100 dashboard mapping for the operator. Anchored at mean_baseline
        # (score 0) so a calm patient reads ~0 even though raw S_t can be
        # slightly negative or positive.
        mild_span = self.thresh_mild - self.mean_baseline
        high_span = self.thresh_high - self.thresh_mild
        if s_t <= self.thresh_mild:
            display_score = 50.0 * ((s_t - self.mean_baseline) / mild_span) if mild_span > 0 else 0.0
        elif s_t <= self.thresh_high:
            display_score = 50.0 + 50.0 * ((s_t - self.thresh_mild) / high_span) if high_span > 0 else 50.0
        else:
            display_score = 100.0

        display_score = max(0.0, min(100.0, display_score))
        return s_t, state, display_score


if __name__ == "__main__":
    # Quick standalone sanity check (post PDF-fix API).
    print("[TEST] Initializing Fusion Engine...")
    fusion = FusionEngine()
    fake_baseline = {'eda': 5.0, 'hr': 75.0, 'hrv': 40.0}
    # Seed per-signal stats by hand for the demo (in production these are
    # computed in calculate_baseline_sigma during the BASELINE phase).
    fusion.eda_phasic_mean, fusion.eda_phasic_sigma = 0.0, 0.05
    fusion.hr_pct_mean, fusion.hr_pct_sigma = 0.0, 3.0
    fusion.hrv_pct_mean, fusion.hrv_pct_sigma = 0.0, 8.0
    fusion.set_thresholds(mean_baseline=0.0, sigma_baseline=0.5)

    stressed_vector = [8.0, 110.0, 20.0]
    fake_phasic = 0.12  # uS — a real phasic event
    for _ in range(50):
        s_inst = fusion.compute_s_instant(stressed_vector, fake_phasic, fake_baseline)
        s_t, state, score = fusion.evaluate_state(s_inst)

    deltas = fusion.compute_deltas(stressed_vector, fake_phasic, fake_baseline)
    print(f"Vector: {stressed_vector}, phasic_eda={fake_phasic} uS")
    print(f"Deltas: EDA {deltas['eda']:+.3f} uS (phasic)  "
          f"HR {deltas['hr']:+.1f}%  HRV {deltas['hrv']:+.1f}%")
    print(f"S_t={s_t:.2f}  state={state.upper()}  score={score:.0f}/100")
