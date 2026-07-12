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
import math
import numpy as np
from config import Config


def _is_nan(x):
    """True iff x is exactly NaN (warm-up sentinel for HR / HRV)."""
    return isinstance(x, float) and math.isnan(x)


class FusionEngine:
    """Stress index, classification, and 0-100 display score."""

    # Fallback sigma when baseline computation degenerates (e.g. a sensor was
    # flat for the whole 120 s). Keeps the system in a usable state instead
    # of locking thresholds at 0, which would label every tick ultra-stressed.
    SIGMA_FALLBACK = 1.5

    def __init__(self):
        # Rolling S_instant buffer whose mean is S_t. Width = S_T_SMOOTH_SEC
        # seconds at PIPELINE_RATE. Kept at a minimum of 1 tick so a
        # misconfigured 0-second value still produces a valid deque.
        self._s_t_window = max(1, int(Config.S_T_SMOOTH_SEC * Config.PIPELINE_RATE))
        self.s_instant_buffer = collections.deque(maxlen=self._s_t_window)

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
        # Use nanmean/nanstd because the cleaned baseline buffers can contain
        # NaN samples from the HR/HRV warm-up period (PDF §3). The per-signal
        # mean and sigma are computed only over real measurements.
        hr_arr_np = np.asarray(hr_arr[:n], dtype=np.float64)
        hrv_arr_np = np.asarray(hrv_arr[:n], dtype=np.float64)
        hr_pct = (hr_arr_np - avg_hr) / avg_hr * 100.0           # NaN-preserving
        hrv_pct = (avg_hrv - hrv_arr_np) / avg_hrv * 100.0        # NaN-preserving

        # ---- Per-signal sigma floors (vret_server.py guardrail) ----
        # The measured baseline sigma is the natural reference. But on a
        # short / unusually flat baseline it can collapse to an implausibly
        # small value — and z = delta / sigma then makes a trivial 1% live
        # wobble look like "ultra-stressed". We refuse to believe a resting
        # signal varies less than its real physiological minimum, so the
        # measured sigma is clamped UP to the per-signal floor below if it
        # falls under it. Normal baselines (HR sigma ~2-3%, HRV ~9%, EDA
        # phasic ~0.05 µS) sit well above their floors — these only bite
        # on broken / flukey ones.
        eda_sigma_measured = float(np.nanstd(phasic_arr)) if phasic_arr.size else 0.0
        hr_sigma_measured = float(np.nanstd(hr_pct)) if np.any(np.isfinite(hr_pct)) else 0.0
        hrv_sigma_measured = float(np.nanstd(hrv_pct)) if np.any(np.isfinite(hrv_pct)) else 0.0

        self.eda_phasic_mean = float(np.nanmean(phasic_arr)) if phasic_arr.size else 0.0
        self.eda_phasic_sigma = max(
            eda_sigma_measured,
            Config.EDA_PHASIC_SIGMA_FLOOR,
            Config.SIGMA_FLOOR)
        # If a buffer is all-NaN (degenerate baseline), nanmean returns NaN.
        # Guard with a fallback so downstream math doesn't see NaN sigma.
        self.hr_pct_mean = float(np.nanmean(hr_pct)) if np.any(np.isfinite(hr_pct)) else 0.0
        self.hr_pct_sigma = max(
            hr_sigma_measured,
            Config.HR_SIGMA_FLOOR_PCT,
            Config.SIGMA_FLOOR)
        self.hrv_pct_mean = float(np.nanmean(hrv_pct)) if np.any(np.isfinite(hrv_pct)) else 0.0
        self.hrv_pct_sigma = max(
            hrv_sigma_measured,
            Config.HRV_SIGMA_FLOOR_PCT,
            Config.SIGMA_FLOOR)

        # Surface a warning if any floor had to engage. Tells the operator
        # the baseline was implausibly flat (likely too short or a sensor
        # issue), even though the floor prevents a false-alarm cascade.
        for _name, _meas, _used in (
            ("HR",         hr_sigma_measured,  self.hr_pct_sigma),
            ("HRV",        hrv_sigma_measured, self.hrv_pct_sigma),
            ("EDA-phasic", eda_sigma_measured, self.eda_phasic_sigma),
        ):
            if _used > _meas + 1e-9:
                print(f"[FUSION] SIGMA-FLOOR: {_name} baseline sigma "
                      f"{_meas:.4f} was below floor; using {_used:.4f}. "
                      f"(Implausibly flat baseline — check collection if "
                      f"this recurs.)")

        n_hr_real = int(np.sum(np.isfinite(hr_pct)))
        n_hrv_real = int(np.sum(np.isfinite(hrv_pct)))
        print(f"[FUSION] Per-signal baselines (for z-scoring):")
        print(f"         EDA phasic: mean={self.eda_phasic_mean:+.4f} uS, "
              f"sigma={self.eda_phasic_sigma:.4f} uS  "
              f"(n={phasic_arr.size})")
        print(f"         HR  delta : mean={self.hr_pct_mean:+.2f}%, "
              f"sigma={self.hr_pct_sigma:.2f}%  "
              f"(n={n_hr_real} real samples)")
        print(f"         HRV delta : mean={self.hrv_pct_mean:+.2f}%, "
              f"sigma={self.hrv_pct_sigma:.2f}%  "
              f"(n={n_hrv_real} real samples after 60 s warm-up)")

        # ---- Pass 2: z-scored S_inst series, only over fully-valid samples ----
        # vret_server.py builds the baseline S_t distribution from windows
        # where ALL three signals are valid measurements. We mirror that:
        # mask in only samples where HR and HRV are both real (EDA is
        # always real). This excludes the 60 s HRV warmup and the first
        # few seconds of HR warmup from the baseline S_t distribution.
        valid_mask = np.isfinite(hr_pct) & np.isfinite(hrv_pct)
        n_valid = int(np.sum(valid_mask))
        if n_valid < int(Config.PIPELINE_RATE):
            print(f"[FUSION] WARN: only {n_valid} fully-valid baseline "
                  f"samples after HR/HRV warmup. Baseline ran too short "
                  f"or the participant was unsettled. Thresholds will be "
                  f"unreliable. Increase BASELINE_SEC or extend pre-session "
                  f"settling.")
            return 0.0, self.SIGMA_FALLBACK

        phasic_v = phasic_arr[valid_mask]
        hr_v = hr_pct[valid_mask]
        hrv_v = hrv_pct[valid_mask]

        z_eda = (phasic_v - self.eda_phasic_mean) / self.eda_phasic_sigma
        z_hr = (hr_v - self.hr_pct_mean) / self.hr_pct_sigma
        z_hrv = (hrv_v - self.hrv_pct_mean) / self.hrv_pct_sigma
        # Phasic can rarely produce NaN at window edges — guard defensively.
        z_eda = np.where(np.isfinite(z_eda), z_eda, 0.0)
        s_instant_series = (Config.WEIGHT_EDA * z_eda
                            + Config.WEIGHT_HRV * z_hrv
                            + Config.WEIGHT_HR * z_hr)

        # Use the same S_t window as the live path so mean_baseline centres
        # on the exact distribution the classifier sees at runtime.
        window = max(1, int(Config.S_T_SMOOTH_SEC * Config.PIPELINE_RATE))
        kernel = np.ones(window, dtype=np.float64) / window
        s_t_series = np.convolve(s_instant_series, kernel, mode='valid')

        # PDF Bug 7: sigma from RAW s_inst (true spread), mean from the
        # smoothed s_t series (centring matches what live classification
        # operates on after the rolling mean).
        sigma_raw = float(np.std(s_instant_series))
        sigma_smoothed = float(np.std(s_t_series))
        mean_baseline = float(np.mean(s_t_series))
        print(f"[FUSION] Baseline S_t (over {n_valid} fully-valid samples): "
              f"mean={mean_baseline:+.4f}, sigma_raw={sigma_raw:.4f} (used), "
              f"sigma_smoothed={sigma_smoothed:.4f} (reference only)")
        return mean_baseline, sigma_raw

    def compute_deltas(self, live_vector: list, phasic_eda: float,
                       baseline_averages: dict) -> dict:
        """
        Per-signal display values for the LSL stream and the live transcript.
        After the PDF math fix, the EDA quantity is phasic EDA in microsiemens
        (tonic-removed, what drives the score). HR and HRV remain percent
        deviations from baseline. Returns NaN for HR or HRV when the data
        source is still warming up — the dashboard / live-log can render
        "—" rather than a fake number, and compute_s_instant handles the
        NaN per the PDF rules (see that function).

        Note: channel 6 of the LSL stream (`delta_eda`) carries phasic EDA
        in µS, not a percent. Documented in OUTPUTS.md.
        """
        eda, hr, hrv = live_vector
        base_hr = baseline_averages['hr'] or 1e-6
        base_hrv = baseline_averages['hrv'] or 1e-6
        d_hr = (float('nan') if _is_nan(hr)
                else ((hr - base_hr) / base_hr) * 100.0)
        d_hrv = (float('nan') if _is_nan(hrv)
                 else ((base_hrv - hrv) / base_hrv) * 100.0)
        return {
            'eda': float(phasic_eda),  # phasic µS, not percent
            'hr':  d_hr,
            'hrv': d_hrv,
        }

    def compute_s_instant(self, live_vector: list, phasic_eda: float,
                           baseline_averages: dict) -> float:
        """
        Z-scored, weighted composite stress score (PDF §2 + vret_server.py):

            S_inst = 0.5 · z(phasic_EDA) + 0.3 · z(HRV%) + 0.2 · z(HR%)

        Each signal is in units of its own baseline spread before the
        weights are applied, so EDA's larger raw magnitude no longer
        dominates by scale alone (PDF §7 Cause 2).

        Warm-up handling — two distinct regimes (PDF §3 + vret_server.py):

          HRV warm-up (first ~60 s of live, before the RMSSD window has
          filled): delta_HRV is HELD AT THE BASELINE MEAN → z_HRV = 0.
          The HRV term is INCLUDED in the weighted sum (contributing 0).
          Weights stay fixed at 0.5 / 0.3 / 0.2 — NO renormalisation.
          Quote from PDF: "delta-HRV is held at 0 (HRV assumed at its
          baseline average) so the weights stay fixed and the score keeps
          the same scale."

          HR warm-up (first ~3 s of live, before the R-peak detector has
          seen enough beats to compute HR): the HR term is OMITTED from
          the sum entirely, and the remaining z-scored terms are
          RENORMALISED so the score keeps the same scale. From
          vret_server.py:
              wsum = sum(w for w, _ in terms)
              s_inst = sum(w * z) / wsum * (W_EDA + W_HRV + W_HR)
          During EDA-only-and-HRV-bootstrap-and-HR-warmup, wsum = 0.8,
          total = 1.0, so the available terms are scaled by 1.25.

        Why the asymmetry?
          * HRV warmup is by DESIGN (60 s minimum window) and lasts a
            full minute. Renormalising over EDA+HR for a whole minute
            would over-amplify them on the wrong scale.
          * HR warmup is INCIDENTAL (first 2-3 s before the detector finds
            beats) and transient. Renormalising for those seconds keeps
            the score on the right scale until HR comes online.
        """
        d = self.compute_deltas(live_vector, phasic_eda, baseline_averages)
        eda_v, hr_v, hrv_v = live_vector

        terms = []  # list of (weight, z-score)

        # ---- EDA: always present (raw EDA is direct from the sensor) ----
        if math.isfinite(d['eda']):
            z_eda = (d['eda'] - self.eda_phasic_mean) / self.eda_phasic_sigma
            terms.append((Config.WEIGHT_EDA, z_eda))

        # ---- HRV: bootstrap to baseline mean (z=0) during warmup ----
        # delta_HRV is NaN during the 60 s warmup. We substitute the baseline
        # mean here so z_HRV = 0; the term is included in the sum so the
        # weights stay fixed at 0.5/0.3/0.2 (no renormalisation, PDF §3).
        if _is_nan(hrv_v):
            terms.append((Config.WEIGHT_HRV, 0.0))
        else:
            z_hrv = (d['hrv'] - self.hrv_pct_mean) / self.hrv_pct_sigma
            if math.isfinite(z_hrv):
                terms.append((Config.WEIGHT_HRV, z_hrv))

        # ---- HR: omit term entirely during the first-few-seconds warmup ----
        # Then renormalise so the score keeps the right scale (vret_server.py).
        if not _is_nan(hr_v):
            z_hr = (d['hr'] - self.hr_pct_mean) / self.hr_pct_sigma
            if math.isfinite(z_hr):
                terms.append((Config.WEIGHT_HR, z_hr))

        if not terms:
            return 0.0

        wsum = sum(w for w, _ in terms)
        weighted = sum(w * z for w, z in terms)
        total = Config.WEIGHT_EDA + Config.WEIGHT_HRV + Config.WEIGHT_HR
        return weighted / wsum * total

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

        # Wait until the buffer has one full S_t window of data before
        # evaluating. Under a 3 s window that is 30 samples at 10 Hz.
        if len(self.s_instant_buffer) < self._s_t_window:
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
