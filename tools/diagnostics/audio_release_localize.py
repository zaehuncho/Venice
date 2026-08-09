#!/usr/bin/env python3
"""Audio release detector — tests whether the shot SFX in the audio track
is a sharper release signal than any visual cue.

The Remote Play stream carries audio. The shot has a distinct release/shot SFX.
Audio is ~1ms resolution vs 16ms video — potentially razor-sharp.

Extracts the audio track, computes a spectral template around meter-confirmed
shots, then cross-correlates to find the release SFX and measures its
localizability vs the meter edge.

Usage:
    C:\\Python314\\python.exe tools/diagnostics/audio_release_localize.py "<video>" [count] [start] [handed] [--meter_color Red]
"""
from __future__ import annotations

import argparse
import os
import sys

import numpy as np
import cv2
from scipy import signal as scipy_signal
from scipy.io import wavfile

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from meter_detector import MeterDetector, load_detector_config

DEBOUNCE = 18


def extract_audio(video_path, start_frame, count, fps):
    """Extract audio from video file, return (samples, sample_rate) aligned to the frame window."""
    # Use ffmpeg to extract audio
    import subprocess
    import tempfile

    tmp_wav = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, dir=os.environ.get("TEMP", "/tmp"))
    tmp_wav.close()

    start_time = start_frame / fps
    duration = count / fps

    cmd = [
        "ffmpeg", "-y", "-ss", f"{start_time:.3f}", "-t", f"{duration:.3f}",
        "-i", video_path, "-vn", "-ac", "1", "-ar", "48000",
        "-f", "wav", tmp_wav.name
    ]

    try:
        subprocess.run(cmd, capture_output=True, timeout=60, check=True)
    except Exception as e:
        print(f"  [error] ffmpeg failed: {e}")
        os.unlink(tmp_wav.name)
        return None, None

    try:
        sr, audio = wavfile.read(tmp_wav.name)
        os.unlink(tmp_wav.name)
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        elif audio.dtype == np.int32:
            audio = audio.astype(np.float32) / 2147483648.0
        return audio, sr
    except Exception as e:
        print(f"  [error] wav read failed: {e}")
        if os.path.exists(tmp_wav.name):
            os.unlink(tmp_wav.name)
        return None, None


def compute_spectrogram(audio, sr, hop_ms=1.0):
    """Compute a mel spectrogram with fine temporal resolution."""
    hop_length = int(sr * hop_ms / 1000.0)
    n_fft = 2048

    # Compute STFT
    freqs, times, Sxx = scipy_signal.spectrogram(
        audio, fs=sr, nperseg=n_fft, noverlap=n_fft - hop_length,
        window="hann", scaling="spectrum", mode="magnitude"
    )

    # Convert to dB
    Sxx_db = 20 * np.log10(Sxx + 1e-10)

    return Sxx_db, freqs, times, hop_ms


def find_shot_sfx(audio, sr, shot_frame_times, fps, search_before_ms=500, search_after_ms=500):
    """For each shot, extract the audio segment around the meter edge.
    Build a template by averaging, then cross-correlate to find the SFX.

    Returns per-shot SFX offset (ms) from the meter edge.
    """
    hop_ms = 1.0
    Sxx_db, freqs, times, hop_ms = compute_spectrogram(audio, sr, hop_ms)

    # Convert frame times to audio sample indices
    # times array is in seconds from spectrogram
    # shot_frame_times are in seconds from the start of the window

    offsets = []

    # Build template: average spectrogram segments around shots
    search_before_s = search_before_ms / 1000.0
    search_after_s = search_after_ms / 1000.0

    templates = []
    shot_time_indices = []

    for shot_t in shot_frame_times:
        lo = shot_t - search_before_s
        hi = shot_t + search_after_s
        t_mask = (times >= lo) & (times <= hi)
        if np.sum(t_mask) < 10:
            continue
        seg = Sxx_db[:, t_mask]
        templates.append(seg)
        shot_time_indices.append(shot_t)

    if len(templates) < 5:
        print(f"  [warn] only {len(templates)} shots with audio segments")
        return []

    # Align and average templates (normalize each)
    min_cols = min(t.shape[1] for t in templates)
    aligned = np.array([t[:, :min_cols] for t in templates])
    template = np.median(aligned, axis=0)

    # For each shot, cross-correlate its segment with the template
    # The peak offset = the SFX timing relative to the meter edge
    for i, shot_t in enumerate(shot_time_indices):
        lo = shot_t - search_before_s
        hi = shot_t + search_after_s
        t_mask = (times >= lo) & (times <= hi)
        if np.sum(t_mask) < 10:
            continue
        seg = Sxx_db[:, t_mask]
        seg_times = times[t_mask]

        # Cross-correlate each frequency bin, then sum
        # Use normalized cross-correlation
        seg_norm = seg - seg.mean(axis=1, keepdims=True)
        tpl_norm = template - template.mean(axis=1, keepdims=True)

        # Sum of per-bin cross-correlations
        n_lag = min(seg.shape[1], template.shape[1])
        corr_curve = np.zeros(seg.shape[1] - n_lag + 1)
        for lag in range(len(corr_curve)):
            window = seg_norm[:, lag:lag + n_lag]
            # Per-bin correlation
            denom = np.sqrt(np.sum(window**2, axis=1) * np.sum(tpl_norm**2, axis=1) + 1e-10)
            corr = np.sum(window * tpl_norm, axis=1) / denom
            corr_curve[lag] = np.nanmean(corr)

        if len(corr_curve) == 0:
            continue

        best_lag = np.argmax(corr_curve)
        # Time of the correlation peak
        peak_time = seg_times[best_lag]
        offset_ms = (peak_time - shot_t) * 1000.0
        offsets.append(offset_ms)

    return offsets


def process_clip(video, count, start, handed, meter_color):
    """Process a clip and measure audio release localizability."""
    mcfg = load_detector_config(os.path.join(ROOT, "settings.json"))
    mcfg.meter_style = "Arrow2"
    mcfg.meter_color = meter_color
    mcfg.auto_meter_color = False
    meter = MeterDetector(os.path.join(ROOT, "meter_styles"), mcfg)
    meter.set_active_style("Arrow2")

    cap = cv2.VideoCapture(video)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60.0
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)

    print(f"  {os.path.basename(video)} | {fps:.0f}fps | window {start}..{start+count}")

    # Detect meter edges (visual)
    shot_edges = []
    prev_meter = False
    last_shot = -10**9
    n = 0

    print("  Detecting meter edges...", end=" ", flush=True)
    while n < count:
        ok, frame = cap.read()
        if not ok:
            break
        seq = start + n
        r = meter.detect(frame)
        fed = bool(r.detected and r.rejection_reason in ("", "green_not_found"))
        if fed and not prev_meter and (seq - last_shot) > DEBOUNCE:
            shot_edges.append(n)
            last_shot = seq
        prev_meter = fed
        n += 1
    cap.release()
    print(f"{len(shot_edges)} shots")

    # Extract audio
    print("  Extracting audio...", end=" ", flush=True)
    audio, sr = extract_audio(video, start, count, fps)
    if audio is None:
        print("FAILED — no audio track or ffmpeg error")
        return {}
    print(f"done | {len(audio)} samples @ {sr}Hz ({len(audio)/sr:.1f}s)")

    # Convert shot frame indices to times (seconds from window start)
    shot_times = [e / fps for e in shot_edges]

    # Find shot SFX via spectrogram cross-correlation
    print("  Computing spectrogram + cross-correlation...", end=" ", flush=True)
    offsets = find_shot_sfx(audio, sr, shot_times, fps)
    print(f"done | {len(offsets)} matched")

    if len(offsets) < 5:
        print("  [warn] too few matched shots")
        return {}

    # Statistics
    arr = np.array(sorted(offsets))
    med = np.median(arr)
    iqr = np.percentile(arr, 75) - np.percentile(arr, 25)
    std = np.std(arr)
    w40 = sum(1 for x in offsets if abs(x - med) <= 40)
    w80 = sum(1 for x in offsets if abs(x - med) <= 80)

    print(f"\n  AUDIO RELEASE LOCALIZABILITY:")
    print(f"  n={len(offsets)} median={med:+.0f}ms IQR={iqr:.0f}ms STD={std:.0f}ms")
    print(f"  ±40ms={w40}/{len(offsets)} ({100*w40//max(len(offsets),1)}%) ±80ms={w80}/{len(offsets)} ({100*w80//max(len(offsets),1)}%)")

    # Also try: use the raw audio envelope (energy spike) instead of spectrogram
    print("\n  Also testing raw audio energy spike:")
    # Compute energy envelope
    hop = int(sr * 0.001)  # 1ms hop
    energy = []
    for i in range(0, len(audio) - hop, hop):
        e = np.sqrt(np.mean(audio[i:i+hop]**2))
        energy.append(e)
    energy = np.array(energy)
    energy_times = np.arange(len(energy)) * hop / sr  # seconds

    # For each shot, find the energy spike near the meter edge
    energy_offsets = []
    search_ms = 500
    search_s = search_ms / 1000.0

    for shot_t in shot_times:
        lo = shot_t - search_s
        hi = shot_t + search_s
        mask = (energy_times >= lo) & (energy_times <= hi)
        if np.sum(mask) < 10:
            continue
        seg_e = energy[mask]
        seg_t = energy_times[mask]

        # Find the sharpest energy rise (max derivative)
        if len(seg_e) > 5:
            deriv = np.diff(seg_e)
            # Find the max positive derivative (sudden onset)
            peak_idx = np.argmax(deriv)
            peak_time = seg_t[peak_idx]
            offset_ms = (peak_time - shot_t) * 1000.0
            energy_offsets.append(offset_ms)

    if len(energy_offsets) >= 5:
        arr2 = np.array(sorted(energy_offsets))
        med2 = np.median(arr2)
        iqr2 = np.percentile(arr2, 75) - np.percentile(arr2, 25)
        std2 = np.std(arr2)
        w40_2 = sum(1 for x in energy_offsets if abs(x - med2) <= 40)
        w80_2 = sum(1 for x in energy_offsets if abs(x - med2) <= 80)
        print(f"  Energy spike: n={len(energy_offsets)} median={med2:+.0f}ms IQR={iqr2:.0f}ms STD={std2:.0f}ms")
        print(f"  ±40ms={w40_2}/{len(energy_offsets)} ({100*w40_2//max(len(energy_offsets),1)}%) ±80ms={w80_2}/{len(energy_offsets)} ({100*w80_2//max(len(energy_offsets),1)}%)")

        return {
            "spectrogram": {"n": len(offsets), "median": med, "iqr": iqr, "within_40": w40, "within_80": w80},
            "energy": {"n": len(energy_offsets), "median": med2, "iqr": iqr2, "within_40": w40_2, "within_80": w80_2},
        }
    else:
        print(f"  Energy spike: only {len(energy_offsets)} matched")

    return {"spectrogram": {"n": len(offsets), "median": med, "iqr": iqr, "within_40": w40, "within_80": w80}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("count", type=int, nargs="?", default=3000)
    ap.add_argument("start", type=int, nargs="?", default=11940)
    ap.add_argument("handed", nargs="?", default="Right")
    ap.add_argument("--meter_color", default="Red")
    args = ap.parse_args()

    print(f"AUDIO RELEASE LOCALIZABILITY TEST")
    print(f"Target: <80ms IQR (decision gate for input-hook build)")
    print(f"{'='*70}")

    results = process_clip(args.video, args.count, args.start, args.handed, args.meter_color)

    print(f"\n{'='*70}")
    print("VERDICT:")
    best_iqr = min((r["iqr"] for r in results.values()), default=9999)
    if best_iqr < 80:
        print(f"  BEST IQR = {best_iqr:.0f}ms < 80ms → ESCALATE to input-hook build!")
    elif best_iqr < 200:
        print(f"  BEST IQR = {best_iqr:.0f}ms < 200ms → marginal, worth pursuing")
    else:
        print(f"  BEST IQR = {best_iqr:.0f}ms ≥ 200ms → audio also floored")


if __name__ == "__main__":
    main()
