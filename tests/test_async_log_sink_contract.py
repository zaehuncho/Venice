from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROLLER_CPP = ROOT / "native_orion" / "src" / "OrionAppController.cpp"
CONTROLLER_H = ROOT / "native_orion" / "src" / "OrionAppController.h"
SINK_CPP = ROOT / "native_orion" / "src" / "OrderedFileLogSink.cpp"
SINK_H = ROOT / "native_orion" / "src" / "OrderedFileLogSink.h"


def _function_body(source: str, signature: str, next_signature: str) -> str:
    start = source.index(signature)
    end = source.index(next_signature, start)
    return source[start:end]


def test_gui_log_flush_only_hands_diagnostic_batch_to_worker() -> None:
    source = CONTROLLER_CPP.read_text(encoding="utf-8")
    body = _function_body(
        source,
        "void OrionAppController::flushPendingLogs()",
        "bool OrionAppController::saveConfigSilently",
    )

    assert "appLogSink_.enqueue(diskBatch)" in body
    assert "QFile file(logPath)" not in body
    assert "QDir(rootDir_).mkpath" not in body
    assert "rotateAppLogIfNeeded" not in body
    assert "QTextStream out" not in body


def test_sink_contract_is_ordered_bounded_and_lossless_at_shutdown() -> None:
    header = SINK_H.read_text(encoding="utf-8")
    source = SINK_CPP.read_text(encoding="utf-8")
    controller = CONTROLLER_H.read_text(encoding="utf-8")

    assert "std::deque<PendingChunk> queue_" in header
    assert "maxOutstandingBytes" in header
    assert "std::thread worker_" in header
    assert "std::mutex enqueueOrderMutex_" in header
    assert "capacityAvailable_.wait" in source
    assert "worker_.join()" in source
    assert "while (!file.flush())" in source
    assert "OrderedFileLogSink appLogSink_" in controller
    assert "logsDirReady_" not in controller
    assert "appLogSizeCheckCountdown_" not in controller


def test_controller_explicitly_drains_and_joins_sink_during_shutdown() -> None:
    source = CONTROLLER_CPP.read_text(encoding="utf-8")
    destructor = _function_body(
        source,
        "OrionAppController::~OrionAppController()",
        "void OrionAppController::requestApplicationShutdown",
    )
    shutdown = _function_body(
        source,
        "void OrionAppController::prepareForApplicationExit()",
        "void OrionAppController::killChiakiProcesses",
    )

    assert "appLogSink_.stopAndDrain()" in destructor
    assert "appLogSink_.drain()" in shutdown
