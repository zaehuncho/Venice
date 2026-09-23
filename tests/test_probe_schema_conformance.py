"""Bind the wire schema, the fixtures and the consumer together so they cannot drift apart.

Codex's contract correction #3 was that PROBE_SCHEMA_2.md left field names, types, enum values,
units, probability scale and cardinality unspecified -- so a producer had nothing exact to target
and would have to infer the contract from the consumer's behaviour. That is the transcription risk
this project has already paid three times.

`tools/timing/PROBE_SCHEMA_2.json` is the machine-readable answer. This file proves it is real:

  * every stage the CONSUMER knows about appears in the schema, and vice versa -- so a stage cannot
    be added to one without the other;
  * every control-fixture record carries the schema's required fields with the right TYPES -- so a
    fixture cannot quietly stop exercising a field the contract demands;
  * every enum value the fixtures use is declared;
  * the consumer's thresholds and verdict vocabularies equal the schema's -- so the 2 ms fidelity
    threshold and the 25 ms hook bound cannot be changed in one place only.

What it deliberately does NOT do is re-test behaviour. That is what test_probe_audit.py and
test_probe_audit_counterexamples.py are for. This file only checks that the three artefacts agree
about what the contract IS.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import test_probe_audit as T                                          # noqa: E402
from tools.timing import probe_audit as PA                            # noqa: E402

SCHEMA_PATH = ROOT / "tools" / "timing" / "PROBE_SCHEMA_2.json"


@pytest.fixture(scope="module")
def schema():
    with open(SCHEMA_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def _is_int(v):
    """The container writes 64-bit values as decimal strings, so both forms are legal."""
    return PA._i(v) is not None


# ---------------------------------------------------------------------------------------
# the schema and the consumer must know the same stages
# ---------------------------------------------------------------------------------------
def test_every_consumer_stage_is_in_the_schema(schema):
    declared = set(schema["stages"])
    known = set(PA.SHOT_STAGES) | {"experiment_manifest", "clock_bridge"}
    assert known - declared == set(), f"consumer knows stages the schema does not: {known - declared}"


def test_every_schema_stage_is_known_to_the_consumer(schema):
    declared = set(schema["stages"])
    known = set(PA.SHOT_STAGES) | {"experiment_manifest", "clock_bridge"}
    assert declared - known == set(), f"schema declares stages the consumer ignores: {declared - known}"


def test_the_python_emitted_stages_agree(schema):
    py = {n for n, d in schema["stages"].items() if d.get("emitter") == "python"}
    assert py == PA.PYTHON_STAGES


# ---------------------------------------------------------------------------------------
# thresholds and vocabularies must be defined once
# ---------------------------------------------------------------------------------------
def test_the_fidelity_threshold_matches(schema):
    assert schema["thresholds"]["displacement_fidelity_us"]["value"] == PA.DISPLACEMENT_FIDELITY_US


def test_the_hook_bound_matches(schema):
    assert schema["thresholds"]["hook_bound_us"]["value"] == PA.HOOK_BOUND_US


def test_the_verdict_vocabularies_match(schema):
    v = schema["consumer_verdicts"]
    assert set(v["measurement"]) == {PA.COMPLETE, PA.INCOMPLETE}
    assert set(v["adherence"]) == {PA.ADHERENT, PA.NONADHERENT, PA.UNVERIFIABLE}
    assert set(v["outcome"]) == {PA.GRADED, PA.UNGRADED, PA.DISPUTED, PA.UNKNOWN}
    assert set(v["clock"]) == {PA.CLOCK_OK, PA.CLOCK_ENGINE_ONLY, PA.CLOCK_REFUSED}
    assert set(v["gate"]) == {PA.GATE_PASS, PA.GATE_WITHHELD}


def test_the_terminal_and_verdict_enums_match(schema):
    assert set(schema["enums"]["terminal_state"]) == PA.TERMINALS
    assert set(schema["enums"]["verdict"]) == PA.VERDICTS


def test_the_worker_event_enum_matches(schema):
    e = {int(k): v for k, v in schema["enums"]["worker_event"].items()}
    assert e == {PA.W_ARM: "arm_result", PA.W_CLAIMED: "claimed",
                 PA.W_DISPATCH: "dispatch_complete", PA.W_RETARGET: "retarget_result"}


def test_the_name_collision_pair_matches(schema):
    assert set(schema["name_collisions"]) - {"_rule"} == set(PA.COLLIDING_NAMES)


def test_the_lifecycles_owing_execution_are_declared(schema):
    """`released` and `killed` owe a delivered command; a pre-arm cancellation does not."""
    text = json.dumps(schema["stages"]["shot_terminal"]["constraints"])
    for state in PA.NEEDS_EXECUTION:
        assert state in text, f"{state} owes execution evidence but the schema does not say so"


# ---------------------------------------------------------------------------------------
# the control fixture must satisfy the schema it claims to target
# ---------------------------------------------------------------------------------------
STAGE_BUILDERS = {
    "shot_press": lambda: T._press(),
    "native_onset": lambda: T._native_onset(),
    "reader_onset": lambda: T._reader_onset(),
    "probe_assignment": lambda: T._assignment(),
    "schedule_plan": lambda: T._plan(),
    "release_link": lambda: T._release(),
    "shot_verdict": lambda: T._verdict(),
    "shot_terminal": lambda: T._terminal(),
    "experiment_manifest": lambda: T._manifest(),
}


@pytest.mark.parametrize("stage", sorted(STAGE_BUILDERS))
def test_the_control_fixture_carries_every_required_field(schema, stage):
    spec = schema["stages"][stage]
    data = STAGE_BUILDERS[stage]()["data"]
    missing = [k for k in spec["required"] if k not in data]
    assert missing == [], f"{stage} fixture is missing required field(s) {missing}"


@pytest.mark.parametrize("stage", sorted(STAGE_BUILDERS))
def test_the_control_fixture_uses_the_declared_types(schema, stage):
    spec = schema["stages"][stage]
    data = STAGE_BUILDERS[stage]()["data"]
    for field, decl in spec["required"].items():
        v = data.get(field)
        if decl.startswith("int"):
            assert _is_int(v), f"{stage}.{field} declared {decl!r} but fixture has {v!r}"
        elif decl.startswith("string"):
            assert isinstance(v, str), f"{stage}.{field} declared string but fixture has {v!r}"


def test_every_worker_variant_carries_its_conditional_fields(schema):
    cond = schema["stages"]["worker"]["conditional"]
    base = set(schema["stages"]["worker"]["required"])
    for builder, key in ((T._worker, "arm_result|retarget_result"),
                         (T._claim, "claimed"), (T._dispatch, "dispatch_complete")):
        data = builder()["data"]
        assert base - set(data) == set(), f"{key} fixture lacks a base worker field"
        for field in cond[key]:
            if "ON ACCEPTANCE" in cond[key][field] or "on acceptance" in cond[key][field]:
                continue
            assert field in data, f"{key} fixture lacks conditional field {field}"


def test_the_fixture_probability_is_an_exact_rational(schema):
    """`assignment_probability=333` had no defined scale. A rational has one."""
    assert "rational" in schema["stages"]["probe_assignment"]["required"]["assignment_probability"]
    v = T._assignment()["data"]["assignment_probability"]
    assert isinstance(v, str) and "/" in v
    n, d = v.split("/")
    assert n.isdigit() and d.isdigit() and int(d) > 0


@pytest.mark.parametrize("field,enum", [
    ("ownership_source", "ownership_source"),
    ("effective_tempo", None),
])
def test_fixture_enum_values_are_declared(schema, field, enum):
    if enum is None:
        return
    v = T._native_onset()["data"][field]
    assert v in schema["enums"][enum], f"{field}={v!r} is not a declared {enum}"


def test_the_verdict_and_terminal_fixtures_use_declared_values(schema):
    assert T._verdict()["data"]["verdict"] in schema["enums"]["verdict"]
    assert T._verdict()["data"]["attribution"] in schema["enums"]["attribution"]
    assert T._verdict()["data"]["coverage"] in schema["enums"]["coverage"]
    assert T._terminal()["data"]["terminal_state"] in schema["enums"]["terminal_state"]
    assert T._release()["data"]["delivery_mode"] in schema["enums"]["delivery_mode"]


def test_no_stage_carries_a_bare_ms_field(schema):
    """Milliseconds were the source of a unit defect and are not carried by this schema."""
    for stage, spec in schema["stages"].items():
        for field in list(spec.get("required", {})) + list(spec.get("optional", {})):
            assert not field.endswith("_ms"), f"{stage}.{field} carries milliseconds"
    for stage, builder in STAGE_BUILDERS.items():
        for field in builder()["data"]:
            assert not field.endswith("_ms"), f"{stage} fixture carries {field}"


def test_the_release_stage_forbids_the_colliding_name(schema):
    assert "release_seq" not in schema["stages"]["release_link"]["required"]
    assert "native_release_seq" in schema["stages"]["release_link"]["required"]
