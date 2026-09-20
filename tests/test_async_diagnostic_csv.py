"""Deterministic diagnostic backpressure tests; no hardware, live files or sleeps."""
from __future__ import annotations

import ast
import builtins
import logging
import os
import threading
import time
from pathlib import Path
from types import MethodType, SimpleNamespace

import pytest

from async_diagnostic_csv import AsyncDiagnosticCsv


class GateFile:
    def __init__(self, *, fail_row=False, fail_flush=False, fail_close=False):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed_event = threading.Event()
        self.parts = []
        self.thread_ids = []
        self.fail_row = fail_row
        self.fail_flush = fail_flush
        self.fail_close = fail_close

    def write(self, text):
        self.thread_ids.append(threading.get_ident())
        if text.startswith("row"):
            self.entered.set()
            assert self.release.wait(3), "test failed to release the fake disk"
            if self.fail_row:
                raise OSError("private exception text must not be reported")
        self.parts.append(text)
        return len(text)

    def flush(self):
        self.thread_ids.append(threading.get_ident())
        if self.fail_flush:
            raise OSError("flush failed")

    def close(self):
        self.thread_ids.append(threading.get_ident())
        self.closed_event.set()
        if self.fail_close:
            raise OSError("close failed")


def sink_for(tmp_path, file, **kw):
    return AsyncDiagnosticCsv(tmp_path / "detframes.csv", "header", opener=lambda *a, **k: file, **kw)


def test_actual_orchestrator_sink_write_returns_while_disk_is_blocked(tmp_path, monkeypatch):
    """Execute the production initialization AST and its actual write contract.

    DETCSV_SOURCE_ROOT permits an unchanged source copy to reproduce the old
    synchronous callback without importing hardware/capture constructors.
    """
    source_root = Path(os.environ.get("DETCSV_SOURCE_ROOT", Path(__file__).resolve().parents[1]))
    source = source_root / "remote_play_orchestrator.py"
    tree = ast.parse(source.read_text(encoding="utf-8-sig"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RemotePlayOrchestrator")
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    def assigned(n, attr):
        return isinstance(n, ast.Assign) and any(isinstance(t, ast.Attribute) and t.attr == attr for t in n.targets)
    a = next(i for i,n in enumerate(init.body) if assigned(n,"_detcsv"))
    b = next(i for i,n in enumerate(init.body) if assigned(n,"_framedump_enabled"))
    namespace = {"os":os,"time":time,"__file__":str(tmp_path / "remote_play_orchestrator.py"),
                 "logger":logging.getLogger("csv-test"),"_DETCSV_HEADER":"header",
                 "AsyncDiagnosticCsv":AsyncDiagnosticCsv}
    obj = SimpleNamespace()
    for method in cls.body:
        if isinstance(method,ast.FunctionDef) and method.name == "_start_detcsv":
            exec(compile(ast.Module(body=[method],type_ignores=[]),str(source),"exec"),namespace)
            obj._start_detcsv = MethodType(namespace[method.name],obj)
    namespace["self"] = obj
    file=GateFile()
    monkeypatch.setenv("ORION_DETCSV","1")
    monkeypatch.setattr(builtins,"open",lambda *a,**k:file)
    exec(compile(ast.Module(body=init.body[a:b],type_ignores=[]),str(source),"exec"),namespace)
    completed=threading.Event()
    producer=threading.Thread(target=lambda:(obj._detcsv.write("row,12.345,source-clock\n"),completed.set()))
    producer.start()
    try:
        assert file.entered.wait(1)
        assert completed.wait(0.1), "disk write blocked the production capture sink callback"
    finally:
        file.release.set()
        producer.join(1)
        if isinstance(obj._detcsv,AsyncDiagnosticCsv):
            assert obj._detcsv.close(1)
        else:
            obj._detcsv.close()


def test_full_queue_drops_new_rows_and_keeps_fifo_without_producer_io(tmp_path):
    f=GateFile(); events=[]; sink=sink_for(tmp_path,f,capacity=2,report=events.append)
    producer_id=threading.get_ident()
    try:
        assert sink.write("row1\n"); assert f.entered.wait(1)
        assert sink.write("row2\n"); assert sink.write("row3\n")
        assert not sink.write("row4\n")
        s=sink.snapshot()
        assert (s["accepted"],s["queued"],s["inflight"],s["dropped_full"])==(3,2,1,1)
    finally:
        f.release.set(); assert sink.close(1)
    assert f.parts==["header\n","row1\n","row2\n","row3\n"]
    assert sink.snapshot()["written"]==3
    assert producer_id not in f.thread_ids
    assert any("drop_full=1" in e for e in events)


def test_close_timeout_discards_pending_and_never_closes_inflight_file(tmp_path):
    f=GateFile();sink=sink_for(tmp_path,f,capacity=2)
    try:
        assert sink.write("row1\n"); assert f.entered.wait(1)
        assert sink.write("row2\n"); assert sink.write("row3\n")
        assert not sink.close(0.01)
        assert not f.closed_event.is_set()
        assert sink.snapshot()["dropped_shutdown"]==2
        assert not sink.write("row4\n")
    finally:
        f.release.set();assert sink.close(1)
    assert f.parts==["header\n","row1\n"]
    assert sink.close(0.0)


def test_reconnect_reports_disabled_when_previous_writer_is_still_closing(tmp_path, caplog):
    source_root=Path(os.environ.get("DETCSV_SOURCE_ROOT",Path(__file__).resolve().parents[1]))
    source=source_root/"remote_play_orchestrator.py"
    cls=next(n for n in ast.parse(source.read_text(encoding="utf-8-sig")).body
             if isinstance(n,ast.ClassDef) and n.name=="RemotePlayOrchestrator")
    method=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=="_start_detcsv")
    f=GateFile();sink=sink_for(tmp_path,f)
    replacements=[]
    def unexpected_replacement(*a,**kw):
        replacements.append((a,kw))
        raise AssertionError("second writer must never start while old owner is alive")
    namespace={"os":os,"time":time,"__file__":str(tmp_path/"remote_play_orchestrator.py"),
               "logger":logging.getLogger("csv-reconnect-test"),"_DETCSV_HEADER":"header",
               "AsyncDiagnosticCsv":unexpected_replacement}
    exec(compile(ast.Module(body=[method],type_ignores=[]),str(source),"exec"),namespace)
    orch=SimpleNamespace(_detcsv_enabled=True,_detcsv=sink)
    start=MethodType(namespace["_start_detcsv"],orch)
    try:
        assert sink.write("row1\n");assert f.entered.wait(1)
        assert not sink.close(0.01)
        with caplog.at_level(logging.WARNING,logger="csv-reconnect-test"):
            start()
        assert orch._detcsv is sink and replacements==[]
        assert orch._detcsv_start_status=="disabled_previous_writer_closing"
        assert "DETCSV disabled_this_start reason=previous_writer_still_closing" in caplog.text
        assert "no replacement writer started" in caplog.text
    finally:
        f.release.set();assert sink.close(1)
    # Worker completion does not silently change the explicit per-start status
    # or schedule an automatic background replacement for the reconnect session.
    assert orch._detcsv_start_status=="disabled_previous_writer_closing"
    assert replacements==[]


def test_blocked_open_does_not_block_submission_and_open_failure_disables_recorder(tmp_path):
    entered=threading.Event();release=threading.Event();reports=[]
    def opener(*a,**k):
        entered.set();assert release.wait(3);raise PermissionError("private path must not be reported")
    sink=AsyncDiagnosticCsv(tmp_path/"detframes.csv","header",opener=opener,report=reports.append)
    try:
        assert entered.wait(1)
        assert sink.write("row1\n")
        assert sink.write("row2\n")
    finally:
        release.set();assert sink.close(1)
    s=sink.snapshot()
    assert s["error"]=="PermissionError" and s["dropped_error"]==2
    assert not sink.write("row3\n")
    assert all("private path" not in r for r in reports)


def test_write_failure_counts_inflight_and_pending_without_retry(tmp_path):
    f=GateFile(fail_row=True);reports=[];sink=sink_for(tmp_path,f,report=reports.append)
    try:
        sink.write("row1\n");assert f.entered.wait(1);sink.write("row2\n")
    finally:
        f.release.set();assert sink.close(1)
    s=sink.snapshot()
    assert (s["written"],s["dropped_error"],s["error"])==(0,2,"OSError")
    assert f.parts==["header\n"]
    assert all("private exception" not in r for r in reports)


@pytest.mark.parametrize("failure",["fail_flush","fail_close"])
def test_flush_or_close_error_is_observable_without_escaping(tmp_path,failure):
    f=GateFile(**{failure:True});f.release.set();sink=sink_for(tmp_path,f)
    assert sink.write("row1\n");assert sink.close(1)
    assert sink.snapshot()["error"]=="OSError"
    assert not sink.write("row2\n")


def test_blocked_reporter_does_not_hold_producer_queue_lock(tmp_path):
    entered=threading.Event();release=threading.Event();f=GateFile();f.release.set()
    def report(message):
        entered.set();assert release.wait(3)
    sink=sink_for(tmp_path,f,report=report)
    try:
        assert entered.wait(1)
        assert sink.write("row1\n")
        assert sink.snapshot()["accepted"]==1
    finally:
        release.set();assert sink.close(1)


def test_reporter_failure_does_not_break_writer(tmp_path):
    f=GateFile();f.release.set()
    def report(_):raise RuntimeError("logger broken")
    sink=sink_for(tmp_path,f,report=report)
    assert sink.write("row1\n");assert sink.close(1)
    assert sink.snapshot()["written"]==1 and sink.snapshot()["error"]==""


def test_byte_limit_rotates_at_a_complete_utf8_row_and_keeps_recording(tmp_path):
    """The per-file budget must ROTATE, not end the session.

    REGRESSION 2026-09-14: detframes.csv stopped at 21:46:27 with 213,917 rows -- exactly the
    64 MiB default -- and the last seven minutes of the session (the aborts under investigation)
    were never recorded.
    """
    reports=[];critical=[]
    path=tmp_path/"detframes.csv"
    sink=AsyncDiagnosticCsv(path,"h",max_bytes=9,report=reports.append,
                            report_critical=critical.append)
    sink.write("é,1\n") # 5 UTF-8 bytes; file now 7 including header.
    sink.write("z,2\n") # 4 more bytes would exceed 9 -> rotate, then write.
    assert sink.close(1)
    retired=list(tmp_path.glob("detframes_*_part01.csv"))
    assert len(retired)==1
    assert retired[0].read_bytes()==b"h\n\xc3\xa9,1\n"   # complete rows only, never a split one
    assert path.read_bytes()==b"h\nz,2\n"               # recording continued in a fresh part
    s=sink.snapshot()
    assert (s["written"],s["dropped_limit"],s["parts"],s["error"])==(2,0,2,"")
    assert any("rotated->" in r for r in critical)


def test_recording_stops_only_when_the_part_budget_is_exhausted(tmp_path):
    reports=[]
    path=tmp_path/"detframes.csv"
    sink=AsyncDiagnosticCsv(path,"h",max_bytes=9,max_parts=1,report=reports.append,
                            report_critical=reports.append)
    sink.write("é,1\n")
    sink.write("z,2\n")
    assert sink.close(1)
    assert path.read_bytes()==b"h\n\xc3\xa9,1\n"
    s=sink.snapshot()
    assert (s["bytes_written"],s["written"],s["dropped_limit"],s["error"])==(7,1,1,"CsvByteLimitReached")
    assert not sink.write("row3\n")
    assert any("drop_limit=1" in r for r in reports)
    # Rows offered after the stop are counted, so the log says how much was lost.
    assert sink.snapshot()["dropped_stopped"]==1


def test_a_row_too_large_for_an_empty_file_stops_instead_of_rotating_forever(tmp_path):
    path=tmp_path/"detframes.csv"
    sink=AsyncDiagnosticCsv(path,"h",max_bytes=9)
    sink.write("x"*32+"\n")
    assert sink.close(1)
    assert path.read_bytes()==b"h\n"
    assert sink.snapshot()["error"]=="CsvByteLimitReached"
    assert list(tmp_path.glob("detframes_*_part*.csv"))==[]


def test_close_drains_every_queued_row_within_the_budget(tmp_path):
    """The final rows of a session must reach disk, not the 'pending rows discarded' path."""
    path=tmp_path/"detframes.csv"
    sink=AsyncDiagnosticCsv(path,"h",report_interval=5.0,flush_interval=5.0)
    for i in range(200):
        assert sink.write("row%d\n"%i)
    assert sink.close(2.0)
    lines=path.read_text(encoding="utf-8").splitlines()
    assert lines[0]=="h" and lines[-1]=="row199" and len(lines)==201
    s=sink.snapshot()
    assert (s["written"],s["dropped_shutdown"],s["queued"])==(200,0,0)


def test_rows_are_flushed_on_a_bounded_schedule_while_the_writer_runs(tmp_path):
    """A live session's rows must reach the OS on a bounded beat, not only at close."""
    f=GateFile();f.release.set()
    flushes=[]
    f.flush=lambda: flushes.append(time.monotonic())
    sink=sink_for(tmp_path,f,flush_interval=0.02,report_interval=0.02)
    try:
        assert sink.write("row1\n")
        deadline=time.monotonic()+3.0
        while time.monotonic()<deadline and not flushes:
            time.sleep(0.01)
        assert flushes, "the writer never flushed inside its bounded interval"
    finally:
        assert sink.close(1)


def test_header_larger_than_budget_never_writes_partial_header(tmp_path):
    sink=AsyncDiagnosticCsv(tmp_path/"detframes.csv","large-header",max_bytes=2)
    assert sink.close(1)
    assert (tmp_path/"detframes.csv").read_bytes()==b""
    assert sink.snapshot()["error"]=="CsvByteLimitReached"


def test_same_second_rotation_does_not_overwrite_prior_capture(tmp_path):
    path=tmp_path/"detframes.csv";stamp=1234567890
    path.write_text("old1\n");os.utime(path,(stamp,stamp))
    sink=AsyncDiagnosticCsv(path,"h",keep=2);assert sink.close(1)
    path.write_text("old2\n");os.utime(path,(stamp,stamp))
    sink=AsyncDiagnosticCsv(path,"h",keep=2);assert sink.close(1)
    archives=list(tmp_path.glob("detframes_*.csv"))
    assert len(archives)==2
    assert {p.read_text() for p in archives}=={"old1\n","old2\n"}


def test_retention_and_payload_size_are_bounded(tmp_path):
    f=GateFile();f.release.set();sink=sink_for(tmp_path,f,capacity=1)
    assert not sink.write(b"wrong-type")
    assert not sink.write("x"*16385)
    assert sink.snapshot()["dropped_invalid"]==2
    assert sink.close(1)
    path=tmp_path/"real.csv"
    for i in range(4):
        sink=AsyncDiagnosticCsv(path,f"h{i}",keep=2);assert sink.close(1)
    assert len(list(tmp_path.glob("real_*.csv")))==2


def test_sidecar_manifest_binds_new_root_helper_and_source_changes(tmp_path):
    from tools.sidecar_bundle_manifest import source_hashes,source_digest
    (tmp_path/"native_orion/backend").mkdir(parents=True)
    (tmp_path/"native_orion/backend/autogreen_sidecar.py").write_text("pass\n")
    # READER_SOURCE_INPUTS binds the banner grader's bytes into the manifest, so the fixture must
    # contain it or source_files() fails closed before it ever hashes a root helper.
    (tmp_path/"tools"/"timing").mkdir(parents=True)
    (tmp_path/"tools"/"timing"/"panel_grade.py").write_text("pass\n")
    helper=tmp_path/"async_diagnostic_csv.py";helper.write_text("version = 1\n")
    first=source_hashes(tmp_path)
    assert "async_diagnostic_csv.py" in first
    helper.write_text("version = 2\n")
    assert source_digest(source_hashes(tmp_path))!=source_digest(first)
