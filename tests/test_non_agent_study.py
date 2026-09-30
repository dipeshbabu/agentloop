from __future__ import annotations

import copy

import pytest

np = pytest.importorskip("numpy")

from examples.non_agent_study.data import Components, digest, fold  # noqa: E402
from examples.non_agent_study.models import Model, train  # noqa: E402
from examples.non_agent_study.protocol import _tasks  # noqa: E402
from examples.non_agent_study.workloads import quality_report, run_task  # noqa: E402


def rows():
    return [
        {
            "id": str(index),
            "features": [
                float(index),
                float(index % 3),
                None if index % 4 == 0 else float(index % 2),
                1.0,
            ],
            "label": str(index // 6),
            "group": digest(index),
        }
        for index in range(12)
    ]


@pytest.mark.parametrize("algorithm", ["knn", "centroid", "logistic"])
def test_actual_learned_models_batch_and_row_predictions_agree(algorithm):
    samples = rows()
    parameters = train(samples, algorithm=algorithm, missing_indicators=True)
    model = Model(parameters)
    batch = model.predict(samples)
    serial = [model.predict([row])[0] for row in samples]
    assert [item["label"] for item in batch] == [item["label"] for item in serial]
    assert np.allclose(
        [item["confidence"] for item in batch], [item["confidence"] for item in serial]
    )
    assert all(np.isfinite(item["confidence"]) for item in batch)
    assert parameters["training_count"] == 12
    changed = copy.deepcopy(parameters)
    changed["training_count"] = 99
    with pytest.raises(ValueError, match="hash"):
        Model(changed)


def test_gold_labels_cannot_affect_predictions_after_training():
    samples = rows()
    model = Model(train(samples, algorithm="knn", missing_indicators=True))
    expected = model.predict(samples)
    for row in samples:
        row["label"] = "not a valid label"
    assert model.predict(samples) == expected


def test_components_and_disjoint_tasks_prevent_identity_leakage():
    components = Components()
    components.union(1, 2)
    components.union(3, 2)
    assert components.find(1) == components.find(3)
    assert fold(["component", components.find(1)]) == fold(["component", components.find(3)])
    samples = [
        {"id": str(index), "features": [index], "label": str(index % 2), "group": str(index // 2)}
        for index in range(20)
    ]
    tasks = _tasks(samples, name="banknote", split="held_out", batch_size=2, count=5)
    groups = [row["group"] for task in tasks for row in task["rows"]]
    assert len(groups) == len(set(groups)) == 10


def test_recorded_and_unrecorded_workload_have_same_outputs_and_unknown_usage():
    samples = rows()
    model = Model(train(samples, algorithm="knn", missing_indicators=True))
    cheap = Model(train(samples, algorithm="centroid", missing_indicators=True))
    workload = {
        "id": "banknote",
        "synthetic": True,
        "prepared_sha256": digest(samples),
        "quality": {"minimum_score": 0.90},
    }
    task = {"id": "fixture", "split": "fit", "rows": samples[:8]}
    options = {"repetition": 0, "protocol_hash": "fixture"}
    recorded = run_task(
        workload, task, {"baseline": model, "cheap": cheap}, variant="baseline", **options
    )
    control = run_task(
        workload,
        task,
        {"baseline": model, "cheap": cheap},
        variant="baseline",
        recording=False,
        **options,
    )
    batched = run_task(
        workload, task, {"baseline": model, "cheap": cheap}, variant="batched", **options
    )
    assert recorded.metadata["output"] == control.metadata["output"] == batched.metadata["output"]
    assert len(recorded.events) == 8 and len(batched.events) == 1 and not control.events
    assert recorded.report()["token_status"] == "unavailable"
    assert recorded.report()["cost_status"] == "unknown"
    report = quality_report(workload, task, recorded, batched)
    assert report["candidate_score"] == report["baseline_score"]


def test_multistage_fallback_preserved_by_batching():
    samples = rows()
    models = {
        "primary": Model(train(samples, algorithm="centroid", missing_indicators=True)),
        "fallback": Model(train(samples, algorithm="knn", missing_indicators=True)),
    }
    workload = {"id": "dry-bean", "synthetic": True, "fallback_margin": 1.0}
    task = {"id": "fixture", "split": "fit", "rows": samples[:8]}
    options = {"repetition": 0, "protocol_hash": "fixture"}
    baseline = run_task(workload, task, models, variant="baseline", **options)
    batched = run_task(workload, task, models, variant="batched", **options)
    assert baseline.metadata["output"] == batched.metadata["output"]
    assert any(event.name == "dry-bean.fallback" for event in baseline.events)
    assert any(event.event_id == "fallback-batch" for event in batched.events)


@pytest.mark.parametrize("cheap_family", ["route_to_smaller_model", "batch_model_calls"])
def test_study_executes_and_exports_native_evidence(tmp_path, cheap_family):
    from examples.non_agent_study.data import write_new
    from examples.non_agent_study.export import export
    from examples.non_agent_study.run import execute

    plan = tmp_path / "plan"
    plan.mkdir()
    samples = rows()
    definitions = {}
    for key, algorithm in (("baseline", "knn"), ("cheap", "centroid")):
        value = train(samples, algorithm=algorithm, missing_indicators=True)
        write_new(plan / "models" / (key + ".json"), value)
        definitions[key] = {
            "path": "models/" + key + ".json",
            "sha256": value["sha256"],
            "algorithm": algorithm,
            "version": "1.0",
        }
    tasks = [
        {"id": f"{phase}-{index}", "split": phase, "rows": samples[:8]}
        for phase in ("fit", "held_out")
        for index in range(2)
    ]
    workload = {
        "id": "banknote",
        "category": "synthetic_unit_fixture",
        "synthetic": True,
        "prepared_sha256": digest(samples),
        "models": definitions,
        "tasks": tasks,
        "repetitions": 1,
        "variants": ["batched", "cheap"],
        "quality": {"scorer": "decision", "minimum_score": 0.5},
        "selection": {"batched": "batch_model_calls", "cheap": cheap_family},
        "attribution": {
            "batched": "isolated_batching",
            "cheap": "combined_configuration_unattributed",
        },
        "warmup": {"repetitions": 1, "input": {"id": "warm", "features": samples[0]["features"]}},
    }
    protocol = {
        "schema_version": "1.0",
        "source_revision": "0" * 40,
        "source_hashes": {},
        "numpy_version": np.__version__,
        "synthetic": True,
        "workloads": [workload],
        "bootstrap": {"samples": 10, "seed": 1, "confidence": 0.95},
        "data_kind": "synthetic_unit_fixture",
        "owner_quality_approval": "test-fixture",
        "accounting": {"operating_cost_usd": None},
        "limits": "Unit test only.",
    }
    protocol["sha256"] = digest(protocol)
    write_new(plan / "protocol.json", protocol)
    execute(plan / "protocol.json", tmp_path / "fit", phase="fit")
    execute(plan / "protocol.json", tmp_path / "held_out", phase="held_out")
    result = export(plan, tmp_path / "fit", tmp_path / "held_out", tmp_path / "report")
    assert sum(row["recorded_pairs"] for row in result["workloads"]) == 8
    assert result["selection_counts"]["selected"] > 0
    if cheap_family == "route_to_smaller_model":
        assert result["selection_counts"]["rejected"] > 0
    assert (tmp_path / "report" / "finding-results.json").exists()
    assert (tmp_path / "report" / "calibration-results.json").exists()
    from examples.non_agent_study.reproduce import reconstruct

    assert reconstruct(tmp_path / "report") == result
    from examples.non_agent_study.data import read

    exclusions = read(tmp_path / "report" / "calibration-exclusions.json")
    if cheap_family == "batch_model_calls":
        assert exclusions and all(item["outcome_retained"] for item in exclusions)
    else:
        # Numeric models have unavailable token usage, so no routing advice is emitted.
        assert exclusions == []
    manifest = read(tmp_path / "report" / "calibration-manifest.json")
    assert all(
        item["prediction"]["type"] != "route_to_smaller_model" for item in manifest["registrations"]
    )
    assert all(
        item["outcome"] is None
        for item in manifest["registrations"]
        if item["context"]["intervention"]["configuration"]["variant"] == "cheap"
    )


def test_large_evidence_roundtrips_without_overwriting(tmp_path):
    import random

    from examples.non_agent_study.package import pack_evidence, unpack_evidence

    root = tmp_path / "source"
    root.mkdir()
    data = random.Random(42).randbytes(1_500_000)
    (root / "large.bin").write_bytes(data)
    (root / "note.txt").write_text("public synthetic archive fixture")
    index = tmp_path / "archive"
    pack_evidence(root, index)
    restored = tmp_path / "restored"
    assert unpack_evidence(index / "index.json", restored) == 2
    assert (restored / "large.bin").read_bytes() == data
    with pytest.raises(ValueError, match="must not exist"):
        unpack_evidence(index / "index.json", restored)
