"""Focused regressions for the offline game-feedback banner reader."""

import csv
import datetime
import os

import cv2
import numpy as np

from tools.timing import banner_reader as br


def _frame_with_banner(word="EXCELLENT", selected_outline=True):
    frame = np.zeros((720, 1280, 3), np.uint8)
    label = br.load_label_template()
    lx, ly = 500, 20
    frame[ly:ly + label.shape[0], lx:lx + label.shape[1]] = cv2.cvtColor(
        label, cv2.COLOR_GRAY2BGR)

    mask = br.load_word_templates()[word][0]
    wx = lx + br.LABEL_W // 2 - mask.shape[1] // 2
    wy = ly + 17
    color = (0, 255, 0) if word == "EXCELLENT" else (255, 255, 255)
    target = frame[wy:wy + mask.shape[0], wx:wx + mask.shape[1]]
    target[mask > 0] = color
    if selected_outline:
        # Current captures draw this outline around a made shot's timing cell.
        # Its solid bottom and side rules must not replace/stretch the word.
        cv2.rectangle(frame, (lx, ly), (lx + br.LABEL_W - 1, ly + 31),
                      (0, 255, 0), 1)
    return frame


def test_selected_cell_outline_does_not_hide_excellent():
    read = br.read_frame(
        _frame_with_banner(),
        br.load_label_template(),
        br.load_word_templates(),
        br.load_tempo_template(),
    )

    assert read.word == "EXCELLENT"
    assert read.color == "green"
    assert read.word_score >= br.WORD_THR


def test_scale_calibration_recovers_smaller_session_banner():
    label = br.load_label_template()
    smaller = cv2.resize(label, None, fx=1 / 1.15, fy=1 / 1.15,
                         interpolation=cv2.INTER_AREA)
    frame = np.zeros((720, 1280, 3), np.uint8)
    x, y = 520, 20
    frame[y:y + smaller.shape[0], x:x + smaller.shape[1]] = cv2.cvtColor(
        smaller, cv2.COLOR_GRAY2BGR)

    calibrated = br._calibrate_scale_frames(
        [frame] * 5, label, br.load_tempo_template())

    assert calibrated is not None
    scale, strip = calibrated
    assert scale == 1.15
    assert strip[0] < round(x * scale) < strip[1]


def test_session_uses_capture_clock_not_async_png_write_time(tmp_path):
    first = tmp_path / "f00000_1_raw.png"
    second = tmp_path / "f00001_0_raw.png"
    assert cv2.imwrite(str(first), np.zeros((4, 4, 3), np.uint8))
    assert cv2.imwrite(str(second), np.zeros((4, 4, 3), np.uint8))

    # Deliberately reverse PNG/write chronology relative to capture chronology.
    # A writer queue can do this across a restart; the capture clock is the only
    # correct shot/banner axis.
    os.utime(first, (2_000.0, 2_000.0))
    os.utime(second, (1_999.0, 1_999.0))
    with open(tmp_path / "frames.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(("idx", "t_ms", "t_wall", "detected", "fill_pct", "conf",
                         "green_center_pct", "bbox_x", "bbox_y", "bbox_w", "bbox_h",
                         "rejection", "write_wall"))
        writer.writerow((0, 0, 1_000.0, 1, 0, 0, -1, 0, 0, 0, 0, "", 2_000.0))
        writer.writerow((1, 10, 1_001.0, 0, 0, 0, -1, 0, 0, 0, 0, "", 1_999.0))

    records = list(br.iter_session(str(tmp_path)))

    assert [idx for idx, _, _ in records] == [0, 1]
    assert [dt for _, dt, _ in records] == [
        datetime.datetime.fromtimestamp(1_000.0),
        datetime.datetime.fromtimestamp(1_001.0),
    ]


def test_legacy_session_keeps_png_mtime_fallback(tmp_path):
    frame = tmp_path / "f00000_0_raw.png"
    assert cv2.imwrite(str(frame), np.zeros((4, 4, 3), np.uint8))
    os.utime(frame, (3_000.0, 3_000.0))
    with open(tmp_path / "frames.csv", "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(("idx", "t_ms", "t_wall", "detected", "fill_pct", "conf",
                         "green_center_pct", "bbox_x", "bbox_y", "bbox_w", "bbox_h",
                         "rejection"))
        writer.writerow((0, 0, 1_000.0, 0, 0, 0, -1, 0, 0, 0, 0, ""))

    [(idx, dt, _)] = list(br.iter_session(str(tmp_path)))

    assert idx == 0
    assert dt == datetime.datetime.fromtimestamp(3_000.0)
