"""Tests for strict EvaluationSuite validation and reference resolution."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from skill_evolution.evaluation import (
    EvaluationSuiteError,
    EvaluationSuiteResolver,
    validate_evaluation_suite,
)


ROOT = Path(__file__).resolve().parents[1]


class EvaluationSuiteTests(unittest.TestCase):
    """Keep coverage definitions explicit, approved, and project-local."""

    def _document(self) -> dict[str, object]:
        return json.loads(
            (
                ROOT
                / "evaluation-suites"
                / "document-html-visualizer-v2.json"
            ).read_text(encoding="utf-8")
        )

    def test_repository_suite_is_valid_but_remains_proposed(self) -> None:
        suite = validate_evaluation_suite(self._document())

        self.assertEqual(suite["status"], "proposed")
        self.assertIsNone(suite["approved_by"])
        self.assertEqual(len(suite["task_cases"]), 5)
        self.assertEqual(
            {item["id"] for item in suite["coverage_dimensions"]},
            {"format", "delivery"},
        )

    def test_resolver_validates_all_referenced_task_cases(self) -> None:
        resolved = EvaluationSuiteResolver(
            ROOT / "evaluation-suites",
            project_root=ROOT,
        ).resolve("document-html-visualizer-v2", require_approved=False)

        self.assertEqual(len(resolved.task_cases), 5)
        self.assertEqual(
            resolved.task_cases["document-formats-inline-text-v1"].delivery,
            "inline_text",
        )

    def test_proposed_suite_cannot_gate_real_coverage(self) -> None:
        resolver = EvaluationSuiteResolver(
            ROOT / "evaluation-suites",
            project_root=ROOT,
        )

        with self.assertRaisesRegex(EvaluationSuiteError, "not approved"):
            resolver.resolve("document-html-visualizer-v2")

    def test_contract_rejects_unknown_fields_and_unsafe_paths(self) -> None:
        unknown = self._document()
        unknown["unexpected"] = True
        with self.assertRaisesRegex(EvaluationSuiteError, "unexpected"):
            validate_evaluation_suite(unknown)

        unsafe = self._document()
        unsafe["task_cases"][0]["path"] = "../outside.json"
        with self.assertRaisesRegex(EvaluationSuiteError, "project root"):
            validate_evaluation_suite(unsafe)

    def test_approved_status_requires_both_approval_fields(self) -> None:
        document = deepcopy(self._document())
        document["status"] = "approved"

        with self.assertRaisesRegex(EvaluationSuiteError, "requires"):
            validate_evaluation_suite(document)

    def test_task_conditions_must_cover_declared_dimension_values(self) -> None:
        missing_dimension = deepcopy(self._document())
        del missing_dimension["task_cases"][0]["conditions"]["delivery"]
        with self.assertRaisesRegex(EvaluationSuiteError, "exactly"):
            validate_evaluation_suite(missing_dimension)

        unknown_value = deepcopy(self._document())
        unknown_value["task_cases"][0]["conditions"]["format"] = "rtf"
        with self.assertRaisesRegex(EvaluationSuiteError, "required_values"):
            validate_evaluation_suite(unknown_value)

    def test_reference_identity_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            suites = root / "evaluation-suites"
            cases = root / "task-cases"
            suites.mkdir()
            cases.mkdir()
            document = self._document()
            document["task_cases"] = [
                {
                    "task_case_id": "expected-case",
                    "path": "task-cases/case.json",
                    "conditions": {"format": "md"},
                }
            ]
            document["coverage_dimensions"] = [
                {"id": "format", "required_values": ["md"]}
            ]
            (suites / "document-html-visualizer-v2.json").write_text(
                json.dumps(document), encoding="utf-8"
            )
            (cases / "case.json").write_text(
                json.dumps(
                    {
                        "schema": "task.case.v1",
                        "task_case_id": "different-case",
                        "delivery": "inline_text",
                        "input": {"text": "content"},
                        "expected_artifacts": ["output.html"],
                        "capability_tags": [],
                        "budget": {},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(EvaluationSuiteError, "identity"):
                EvaluationSuiteResolver(
                    suites, project_root=root
                ).resolve(
                    "document-html-visualizer-v2",
                    require_approved=False,
                )


if __name__ == "__main__":
    unittest.main()
