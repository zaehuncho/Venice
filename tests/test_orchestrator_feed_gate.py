"""Engine-feed gate coverage for RemotePlayOrchestrator.

Guards the regression where the engine fed only detections with an EMPTY
rejection_reason — which dropped every rising-phase / contested-shot frame
(rejection_reason='green_not_found', detected=True) so the native engine starved
(presence=no_sample_ever -> max_hold_safety). The gate must feed any real
detection (incl. green_not_found) and exclude ONLY the idle 'meter_memory' echo.
"""
import types

from remote_play_orchestrator import RemotePlayOrchestrator as O


def _res(detected=True, rej="", bbox=(900, 300, 26, 120)):
    return types.SimpleNamespace(detected=detected, rejection_reason=rej, bbox=bbox)


def test_feeds_rising_meter_without_green():
    # A detected, rising meter with no green window yet -> MUST reach the engine.
    assert O._should_feed_engine(_res(rej="green_not_found")) is True


def test_feeds_fully_accepted_detection():
    assert O._should_feed_engine(_res(rej="")) is True


def test_excludes_idle_meter_memory_echo():
    assert O._should_feed_engine(_res(rej="meter_memory")) is False


def test_excludes_unstable_and_lowconf_detections():
    # Jittery/low-confidence detections produce noisy fill that whipsaws the
    # velocity/crossing prediction -> must NOT feed the timing engine.
    assert O._should_feed_engine(_res(rej="bbox_unstable")) is False
    assert O._should_feed_engine(_res(rej="low_confidence")) is False


def test_excludes_undetected_frame():
    assert O._should_feed_engine(_res(detected=False, rej="roi_not_found")) is False


def test_excludes_invalid_bbox():
    assert O._should_feed_engine(_res(rej="", bbox=(0, 0, 0, 0))) is False


def test_handles_missing_result():
    assert O._should_feed_engine(None) is False
