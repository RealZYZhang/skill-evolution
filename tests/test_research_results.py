"""Strict multi-Trajectory specialist result contract tests."""

from __future__ import annotations

import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from skill_evolution.research_results import (
    ResearchResultError,
    validate_error_identification,
    validate_error_identification_evidence,
    validate_error_report,
    validate_error_report_evidence,
    validate_research_result,
    validate_research_result_evidence,
)


CORPUS_DIGEST = hashlib.sha256(b"corpus").hexdigest()
BASELINE_DIGEST = hashlib.sha256(b"baseline").hexdigest()


def _reference(run_id: str, seq: int) -> dict[str, object]:
    return {
        "schema": "evidence.ref.v1",
        "run_id": run_id,
        "seq": seq,
    }


def _result() -> dict[str, object]:
    return {
        "schema": "analysis.multi_trajectory_research.v1",
        "role": "behavior_pattern_analyst",
        "corpus_digest": CORPUS_DIGEST,
        "baseline_digest": BASELINE_DIGEST,
        "research_scope": {
            "eligible_trajectory_ids": ["run-1", "run-2", "run-3"],
            "reviewed_trajectory_ids": ["run-1", "run-2", "run-3"],
            "counterexample_search": "Compared every indexed script action.",
        },
        "findings": [
            {
                "id": "finding-1",
                "subject": "Temporary generation flow",
                "pattern_type": "implicit_behavior",
                "claim": "Two runs created equivalent generators.",
                "eligible_trajectory_ids": ["run-1", "run-2", "run-3"],
                "observed_trajectory_ids": ["run-1", "run-2"],
                "checked_absent_trajectory_ids": ["run-3"],
                "logical_phase": "recovery after the first large write",
                "shared_purpose": "generate the final artifact locally",
                "observable_effect": "a later output write succeeded",
                "confidence": 0.9,
                "evidence": [_reference("run-1", 2), _reference("run-2", 3)],
                "counterevidence": [_reference("run-3", 4)],
                "derivation_ids": ["derive-1"],
                "limitations": ["Script bodies were not byte-identical."],
            }
        ],
        "limitations": [],
    }


class ResearchResultContractTests(unittest.TestCase):
    """Cross-Trajectory findings must classify scope and cite every occurrence."""

    def test_accepts_a_grounded_repeated_pattern(self) -> None:
        result = validate_research_result(
            _result(),
            expected_role="behavior_pattern_analyst",
            expected_corpus_digest=CORPUS_DIGEST,
            expected_baseline_digest=BASELINE_DIGEST,
            allowed_trajectory_ids=["run-1", "run-2", "run-3"],
            known_derivation_ids=["derive-1"],
        )

        self.assertEqual(result["findings"][0]["confidence"], 0.9)

    def test_rejects_single_trajectory_repetition_and_wrong_denominator(self) -> None:
        single = _result()
        finding = single["findings"][0]
        assert isinstance(finding, dict)
        finding["observed_trajectory_ids"] = ["run-1"]
        finding["checked_absent_trajectory_ids"] = ["run-2", "run-3"]
        finding["evidence"] = [_reference("run-1", 2)]
        with self.assertRaisesRegex(ResearchResultError, "two observed"):
            validate_research_result(single, known_derivation_ids=["derive-1"])

        unclassified = _result()
        finding = unclassified["findings"][0]
        assert isinstance(finding, dict)
        finding["checked_absent_trajectory_ids"] = []
        with self.assertRaisesRegex(ResearchResultError, "classify every"):
            validate_research_result(
                unclassified, known_derivation_ids=["derive-1"]
            )

        excluded = _result()
        scope = excluded["research_scope"]
        assert isinstance(scope, dict)
        scope["eligible_trajectory_ids"] = ["run-1", "run-2"]
        scope["reviewed_trajectory_ids"] = ["run-1", "run-2"]
        finding = excluded["findings"][0]
        assert isinstance(finding, dict)
        finding["eligible_trajectory_ids"] = ["run-1", "run-2"]
        finding["checked_absent_trajectory_ids"] = []
        with self.assertRaisesRegex(ResearchResultError, "complete eligible"):
            validate_research_result(
                excluded,
                allowed_trajectory_ids=["run-1", "run-2", "run-3"],
                known_derivation_ids=["derive-1"],
            )

    def test_rejects_missing_trajectory_evidence_and_unknown_derivation(self) -> None:
        missing = _result()
        finding = missing["findings"][0]
        assert isinstance(finding, dict)
        finding["evidence"] = [_reference("run-1", 2)]
        with self.assertRaisesRegex(ResearchResultError, "run-2"):
            validate_research_result(missing, known_derivation_ids=["derive-1"])

        unknown = _result()
        with self.assertRaisesRegex(ResearchResultError, "unknown derivations"):
            validate_research_result(unknown)

    def test_behavior_cannot_shrink_denominator_and_consistency_needs_two(
        self,
    ) -> None:
        narrowed = _result()
        finding = narrowed["findings"][0]
        assert isinstance(finding, dict)
        finding["eligible_trajectory_ids"] = ["run-1", "run-2"]
        finding["checked_absent_trajectory_ids"] = []
        with self.assertRaisesRegex(ResearchResultError, "complete research"):
            validate_research_result(
                narrowed,
                known_derivation_ids=["derive-1"],
            )

        consistency = _result()
        consistency["role"] = "outcome_consistency_analyst"
        finding = consistency["findings"][0]
        assert isinstance(finding, dict)
        finding["pattern_type"] = "inconsistency"
        finding["eligible_trajectory_ids"] = ["run-1", "run-2"]
        finding["observed_trajectory_ids"] = ["run-1"]
        finding["checked_absent_trajectory_ids"] = ["run-2"]
        finding["evidence"] = [_reference("run-1", 2)]
        finding["counterevidence"] = [_reference("run-2", 3)]
        with self.assertRaisesRegex(ResearchResultError, "two observed"):
            validate_research_result(
                consistency,
                known_derivation_ids=["derive-1"],
            )

    def test_rejects_evidence_fields_outside_the_contract(self) -> None:
        value = _result()
        finding = value["findings"][0]
        assert isinstance(finding, dict)
        evidence = finding["evidence"]
        assert isinstance(evidence, list)
        assert isinstance(evidence[0], dict)
        evidence[0]["note"] = "not part of evidence.ref.v1"

        with self.assertRaisesRegex(ResearchResultError, "unexpected fields"):
            validate_research_result(value, known_derivation_ids=["derive-1"])

    def test_resolves_every_reference_against_the_frozen_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for run_id, sequence in (("run-1", 2), ("run-2", 3), ("run-3", 4)):
                trajectory = root / "runs" / run_id / "trajectory.jsonl"
                trajectory.parent.mkdir(parents=True)
                trajectory.write_text(
                    json.dumps({"run_id": run_id, "seq": sequence}) + "\n",
                    encoding="utf-8",
                )
            result = validate_research_result(
                _result(), known_derivation_ids=["derive-1"]
            )

            validate_research_result_evidence(result, bundle_root=root)

            (root / "runs/run-2/trajectory.jsonl").write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ResearchResultError, "seq 3"):
                validate_research_result_evidence(result, bundle_root=root)



def _identification() -> dict[str, object]:
    return {
        "schema": "analysis.error_identification.v1",
        "role": "error_identifier",
        "corpus_digest": CORPUS_DIGEST,
        "baseline_digest": BASELINE_DIGEST,
        "scope": {
            "eligible_trajectory_ids": ["run-1", "run-2", "run-3"],
            "reviewed_trajectory_ids": ["run-1", "run-2", "run-3"],
            "counterexample_search": "Searched every run for error signals.",
        },
        "errors": [
            {
                "error_id": "E1",
                "title": "Large write hits token limit",
                "summary": "First full-HTML write fails with output-token limit.",
                "anchor_evidence": [_reference("run-1", 19)],
                "observed_trajectory_ids": ["run-1", "run-2", "run-3"],
                "checked_absent_trajectory_ids": [],
                "suggested_dimensions": ["behavior", "resource"],
                "notes": "Recovery strategy differs across runs.",
            }
        ],
        "limitations": ["Single input size observed."],
    }


def _report() -> dict[str, object]:
    return {
        "schema": "analysis.error_report.v1",
        "error_id": "E1",
        "role": "error_analyst",
        "corpus_digest": CORPUS_DIGEST,
        "baseline_digest": BASELINE_DIGEST,
        "scope": {
            "eligible_trajectory_ids": ["run-1", "run-2", "run-3"],
            "reviewed_trajectory_ids": ["run-1", "run-2", "run-3"],
            "counterexample_search": "Compared recovery strategies.",
        },
        "dimensions": [
            {
                "dimension": "behavior",
                "claim": "First inline write fails; recovery switches to scripts.",
                "observed_trajectory_ids": ["run-1", "run-2"],
                "checked_absent_trajectory_ids": ["run-3"],
                "evidence": [_reference("run-1", 19), _reference("run-2", 19)],
                "counterevidence": [_reference("run-3", 20)],
                "confidence": 0.9,
                "derivation_ids": ["derive-1"],
                "limitations": ["run-3 recovered differently."],
            }
        ],
        "limitations": ["Only one error analyzed."],
    }


class ErrorIdentificationContractTests(unittest.TestCase):
    """The main agent's error list must be complete, deduplicated, and grounded."""

    def test_accepts_a_grounded_error_list(self) -> None:
        validated = validate_error_identification(
            _identification(),
            expected_corpus_digest=CORPUS_DIGEST,
            expected_baseline_digest=BASELINE_DIGEST,
            allowed_trajectory_ids=["run-1", "run-2", "run-3"],
        )

        self.assertEqual(validated["errors"][0]["error_id"], "E1")
        self.assertEqual(
            validated["errors"][0]["suggested_dimensions"],
            ["behavior", "resource"],
        )

    def test_rejects_bad_denominator_and_duplicate_ids(self) -> None:
        narrowed = _identification()
        error = narrowed["errors"][0]
        assert isinstance(error, dict)
        error["observed_trajectory_ids"] = ["run-1"]
        error["checked_absent_trajectory_ids"] = ["run-2"]
        with self.assertRaisesRegex(ResearchResultError, "classify every"):
            validate_error_identification(narrowed)

        duplicate = _identification()
        assert isinstance(duplicate["errors"], list)
        duplicate["errors"].append(dict(duplicate["errors"][0]))
        with self.assertRaisesRegex(ResearchResultError, "Duplicate error id"):
            validate_error_identification(duplicate)

    def test_rejects_unknown_dimension_hints_and_missing_anchor(self) -> None:
        unknown = _identification()
        error = unknown["errors"][0]
        assert isinstance(error, dict)
        error["suggested_dimensions"] = ["visual"]
        with self.assertRaisesRegex(ResearchResultError, "unknown dimensions"):
            validate_error_identification(unknown)

        unanchored = _identification()
        error = unanchored["errors"][0]
        assert isinstance(error, dict)
        error["anchor_evidence"] = []
        with self.assertRaisesRegex(ResearchResultError, "must not be empty"):
            validate_error_identification(unanchored)

    def test_resolves_anchor_evidence_against_the_frozen_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trajectory = root / "runs/run-1/trajectory.jsonl"
            trajectory.parent.mkdir(parents=True)
            trajectory.write_text(
                json.dumps({"run_id": "run-1", "seq": 19}) + "\n",
                encoding="utf-8",
            )
            validated = validate_error_identification(_identification())

            validate_error_identification_evidence(validated, bundle_root=root)

            trajectory.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ResearchResultError, "seq 19"):
                validate_error_identification_evidence(
                    validated, bundle_root=root
                )


class ErrorReportContractTests(unittest.TestCase):
    """One error report must expose only legitimate, coherent dimensions."""

    def test_accepts_a_problematic_dimension_report(self) -> None:
        validated = validate_error_report(
            _report(),
            expected_corpus_digest=CORPUS_DIGEST,
            expected_baseline_digest=BASELINE_DIGEST,
            allowed_trajectory_ids=["run-1", "run-2", "run-3"],
            known_derivation_ids=["derive-1"],
        )

        self.assertEqual(validated["dimensions"][0]["dimension"], "behavior")

    def test_rejects_unknown_and_duplicate_dimensions(self) -> None:
        unknown = _report()
        dimension = unknown["dimensions"][0]
        assert isinstance(dimension, dict)
        dimension["dimension"] = "performance"
        with self.assertRaisesRegex(ResearchResultError, "unsupported"):
            validate_error_report(unknown)

        duplicate = _report()
        assert isinstance(duplicate["dimensions"], list)
        duplicate["dimensions"].append(dict(duplicate["dimensions"][0]))
        with self.assertRaisesRegex(ResearchResultError, "Duplicate dimension"):
            validate_error_report(
                duplicate, known_derivation_ids=["derive-1"]
            )

    def test_records_missing_trajectory_evidence_as_warnings(self) -> None:
        missing = _report()
        dimension = missing["dimensions"][0]
        assert isinstance(dimension, dict)
        dimension["evidence"] = [_reference("run-1", 19)]
        validated = validate_error_report(
            missing, known_derivation_ids=["derive-1"]
        )

        self.assertIn("validation_warnings", validated)
        warnings = validated["validation_warnings"]
        self.assertTrue(
            any("run-2" in warning for warning in warnings),
            f"expected run-2 coverage warning, got {warnings}",
        )

    def test_rejects_unknown_derivation(self) -> None:
        unknown = _report()
        with self.assertRaisesRegex(ResearchResultError, "unknown derivations"):
            validate_error_report(unknown)

    def test_resolves_dimension_evidence_against_the_frozen_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for run_id, sequence in (("run-1", 19), ("run-2", 19), ("run-3", 20)):
                trajectory = root / "runs" / run_id / "trajectory.jsonl"
                trajectory.parent.mkdir(parents=True)
                trajectory.write_text(
                    json.dumps({"run_id": run_id, "seq": sequence}) + "\n",
                    encoding="utf-8",
                )
            validated = validate_error_report(
                _report(), known_derivation_ids=["derive-1"]
            )

            validate_error_report_evidence(validated, bundle_root=root)

            (root / "runs/run-3/trajectory.jsonl").write_text(
                "", encoding="utf-8"
            )
            with self.assertRaisesRegex(ResearchResultError, "seq 20"):
                validate_error_report_evidence(validated, bundle_root=root)


if __name__ == "__main__":
    unittest.main()
