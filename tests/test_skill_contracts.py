"""Deterministic Skill contract, package, coverage, and CLI tests."""

from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts.skill_contract import _run_cli
from skill_evolution.skill_contracts import (
    SKILL_VALIDATION_REPORT_SCHEMA,
    validate_skill_contract,
)
from skill_evolution.storage import load_json_object


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _contract(*, status: str = "approved") -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "skill.capability_contract.v1",
        "skill_id": "sample-skill",
        "version": "1",
        "status": status,
        "approved_by": None,
        "approved_at": None,
        "capabilities": [
            {
                "id": "inline-text",
                "claim": "Transform inline text.",
                "delivery_modes": ["inline_text"],
                "formats": ["inline_text"],
                "required_evidence": ["output exists"],
            }
        ],
    }
    if status == "approved":
        value["approved_by"] = "project-owner"
        value["approved_at"] = "2026-08-04T00:00:00Z"
    return value


def _write_valid_skill(path: Path) -> None:
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        "---\n"
        "name: Sample Skill\n"
        "description: Transform supplied text.\n"
        "---\n\n"
        "# Sample Skill\n\n"
        "Read the input and write the expected artifact.\n",
        encoding="utf-8",
    )


def _write_inline_case(path: Path, *, identifier: str = "inline-1") -> None:
    _write_json(
        path,
        {
            "schema": "task.case.v1",
            "task_case_id": identifier,
            "delivery": "inline_text",
            "input": {"text": "Hello"},
            "expected_artifacts": ["output.txt"],
            "capability_tags": ["delivery:inline_text"],
            "budget": {},
        },
    )


class SkillContractValidatorTests(unittest.TestCase):
    """Validate structural findings without executing a Skill or model."""

    def test_approved_contract_and_covered_skill_are_dynamic_test_ready(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract_path = root / "contract.json"
            skill = root / "skill"
            task_case = root / "inline.json"
            _write_json(contract_path, _contract())
            _write_valid_skill(skill)
            _write_inline_case(task_case)

            report = validate_skill_contract(
                contract_path=contract_path,
                skill_directory=skill,
                task_case_paths=[task_case],
            )

        self.assertEqual(report["schema"], SKILL_VALIDATION_REPORT_SCHEMA)
        self.assertEqual(report["status"], "valid")
        self.assertTrue(report["valid"])
        self.assertTrue(report["dynamic_test_ready"])
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["coverage_gaps"], [])
        self.assertEqual(
            report["coverage"]["capabilities"][0][
                "covered_by_task_case_ids"
            ],
            ["inline-1"],
        )
        self.assertEqual(
            report["skill"]["front_matter"]["name"],
            "Sample Skill",
        )

    def test_reports_markdown_errors_and_uncovered_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract_path = root / "contract.json"
            skill = root / "skill"
            _write_json(contract_path, _contract())
            skill.mkdir()
            (skill / "SKILL.md").write_text(
                "# Missing front matter\n\n```json\n{}\n",
                encoding="utf-8",
            )

            report = validate_skill_contract(
                contract_path=contract_path,
                skill_directory=skill,
            )

        error_codes = {item["code"] for item in report["errors"]}
        gap_codes = {item["code"] for item in report["coverage_gaps"]}
        self.assertEqual(report["status"], "error")
        self.assertFalse(report["valid"])
        self.assertFalse(report["dynamic_test_ready"])
        self.assertIn("missing_front_matter", error_codes)
        self.assertIn("unclosed_fenced_code_block", error_codes)
        self.assertIn("capability_without_task_case", gap_codes)

    def test_invalid_contract_and_task_case_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract_path = root / "contract.json"
            skill = root / "skill"
            task_case = root / "invalid-task-case.json"
            _write_json(contract_path, {"schema": "unsupported"})
            _write_valid_skill(skill)
            task_case.write_text("{", encoding="utf-8")

            report = validate_skill_contract(
                contract_path=contract_path,
                skill_directory=skill,
                task_case_paths=[task_case],
            )

        error_codes = {item["code"] for item in report["errors"]}
        self.assertEqual(report["status"], "error")
        self.assertIn("skill_contract_invalid", error_codes)
        self.assertIn("task_case_invalid", error_codes)
        self.assertEqual(report["coverage"]["capabilities"], [])

    def test_skill_package_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract_path = root / "contract.json"
            skill = root / "skill"
            task_case = root / "inline.json"
            outside = root / "outside.txt"
            _write_json(contract_path, _contract())
            _write_valid_skill(skill)
            _write_inline_case(task_case)
            outside.write_text("not package content", encoding="utf-8")
            (skill / "linked.txt").symlink_to(outside)

            report = validate_skill_contract(
                contract_path=contract_path,
                skill_directory=skill,
                task_case_paths=[task_case],
            )

        error_codes = {item["code"] for item in report["errors"]}
        self.assertIn("skill_package_symlink", error_codes)
        self.assertFalse(report["dynamic_test_ready"])

    def test_checked_in_skill_contract_is_valid_and_ready(
        self,
    ) -> None:
        task_cases = sorted(
            (PROJECT_ROOT / "task-cases/document-formats").glob("*.json")
        )
        report = validate_skill_contract(
            skill_directory=(
                PROJECT_ROOT / "skills/document-html-visualizer-skill"
            ),
            task_case_paths=task_cases,
        )

        self.assertEqual(report["errors"], [])
        self.assertTrue(report["valid"])
        self.assertEqual(report["status"], "valid")
        self.assertEqual(report["warnings"], [])
        self.assertEqual(
            report["coverage"]["basis"],
            "evaluation_suite_reference",
        )
        self.assertEqual(
            report["coverage"]["suite_refs"],
            ["document-html-visualizer-v2"],
        )
        self.assertTrue(report["dynamic_test_ready"])

    def test_cli_writes_report_and_can_require_owner_approval(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract_path = root / "contract.json"
            skill = root / "skill"
            task_case = root / "inline.json"
            output = root / "report.json"
            _write_json(contract_path, _contract())
            _write_valid_skill(skill)
            _write_inline_case(task_case)

            with redirect_stdout(io.StringIO()):
                status = _run_cli(
                    [
                        "--contract",
                        str(contract_path),
                        "--skill",
                        str(skill),
                        "--task-case",
                        str(task_case),
                        "--output",
                        str(output),
                        "--require-approved",
                    ]
                )
            persisted = load_json_object(output)
            proposed_path = root / "proposed.json"
            _write_json(proposed_path, _contract(status="proposed"))
            with redirect_stdout(io.StringIO()):
                proposed_status = _run_cli(
                    [
                        "--contract",
                        str(proposed_path),
                        "--skill",
                        str(skill),
                        "--task-case",
                        str(task_case),
                        "--require-approved",
                    ]
                )

        self.assertEqual(status, 0)
        self.assertEqual(persisted["schema"], SKILL_VALIDATION_REPORT_SCHEMA)
        self.assertEqual(proposed_status, 1)


if __name__ == "__main__":
    unittest.main()
