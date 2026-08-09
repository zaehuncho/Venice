import copy
import json
from pathlib import Path

import pytest

from tip_registration_infer import RegistrationPredictor
from tools import sidecar_bundle_manifest as sidecar
from tools.timing import build_tip_registration as builder


ROOT = Path(__file__).resolve().parents[1]


def _tip_payload():
    return json.loads((ROOT / "models" / "tip_registration.json").read_text(encoding="utf-8"))


def _factory_payload():
    return json.loads((ROOT / "models" / "latency_factory_prior.json").read_text(encoding="utf-8"))


def test_checked_in_tip_model_matches_builder_and_bundle_contracts():
    payload = _tip_payload()
    builder.validate_payload(payload)
    sidecar.validate_model_document("models/tip_registration.json", payload)
    assert payload["schema"] == builder.SCHEMA
    assert payload["model_id"] == builder.MODEL_ID
    assert payload["model_version"] == builder.model_content_version(payload)

    predictor = RegistrationPredictor()
    assert predictor.enabled, predictor.load_error
    assert predictor.model_id == payload["model_id"]
    assert predictor.model_version == payload["model_version"]
    assert predictor.self_test_report()["ok"] is True


def test_builder_assembles_reproducible_v2_and_runtime_accepts_it(tmp_path):
    source = _tip_payload()
    payload = builder.assemble_payload(
        u_grid=source["u_grid"],
        g_vals=source["g_vals"],
        u_tip=source["u_tip"],
        priors=source["priors"],
        q33=source["q33"],
        q66=source["q66"],
        n_shots=source["n_shots"],
        n_sessions=source["n_sessions"],
        built_utc="2026-08-02T00:00:00Z",
    )
    assert payload["model_version"] == source["model_version"]
    assert payload["uncertainty"] == builder.UNCERTAINTY

    destination = tmp_path / "tip_registration.json"
    builder.write_validated_payload(payload, destination)
    predictor = RegistrationPredictor(str(destination))
    assert predictor.enabled, predictor.load_error
    assert predictor.model_version == payload["model_version"]


def test_tip_model_numeric_tamper_requires_a_new_content_version():
    payload = _tip_payload()
    payload["u_tip"] += 0.001
    with pytest.raises(ValueError, match="content version"):
        builder.validate_payload(payload)
    with pytest.raises(ValueError, match="content version"):
        sidecar.validate_model_document("models/tip_registration.json", payload)


def test_tip_model_rejects_non_monotone_template_even_when_reversioned():
    payload = _tip_payload()
    payload["g_vals"][10] = payload["g_vals"][9] - 0.5
    payload["model_version"] = builder.model_content_version(payload)
    with pytest.raises(ValueError, match="monotone"):
        builder.validate_payload(payload)
    with pytest.raises(ValueError, match="monotone"):
        sidecar.validate_model_document("models/tip_registration.json", payload)


def test_latency_factory_prior_has_valid_pipe_and_fallback_route_coverage():
    payload = _factory_payload()
    sidecar.validate_model_document("models/latency_factory_prior.json", payload)
    signatures = {
        tuple(sorted(term.lower() for term in profile["scope_contains"]))
        for profile in payload["profiles"]
    }
    assert ("capture_card", "controller=pipe") in signatures
    assert ("controller=pipe", "decoder") in signatures
    assert ("capture_card",) in signatures
    assert ("decoder",) in signatures


def test_latency_factory_prior_rejects_ambiguous_duplicate_scope():
    payload = _factory_payload()
    duplicate = copy.deepcopy(payload["profiles"][0])
    duplicate["name"] = "duplicate-profile"
    payload["profiles"].append(duplicate)
    with pytest.raises(ValueError, match="ambiguous"):
        sidecar.validate_model_document("models/latency_factory_prior.json", payload)
