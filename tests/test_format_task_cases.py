"""Tests for the owner-reviewable cross-format TaskCase fixtures."""

from __future__ import annotations

from pathlib import Path
import unittest
import zipfile

from scripts.task_case import load_task_case
from skill_evolution.analysis import load_approved_skill_contract


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FormatTaskCaseTests(unittest.TestCase):
    """Equivalent format fixtures isolate delivery and parsing capability."""

    def test_all_five_delivery_forms_load_with_expected_identity(self) -> None:
        cases_root = PROJECT_ROOT / "task-cases/document-formats"
        cases = {
            path.stem: load_task_case(path)
            for path in sorted(cases_root.glob("*.json"))
        }

        self.assertEqual(
            set(cases),
            {"markdown", "text", "docx", "pdf", "inline-text"},
        )
        self.assertEqual(cases["markdown"].source_path.suffix, ".md")
        self.assertEqual(cases["text"].source_path.suffix, ".txt")
        self.assertEqual(cases["docx"].source_path.suffix, ".docx")
        self.assertEqual(cases["pdf"].source_path.suffix, ".pdf")
        self.assertEqual(cases["inline-text"].delivery, "inline_text")
        self.assertTrue(
            all(
                case.expected_artifacts == ("output.html",)
                for case in cases.values()
            )
        )

    def test_binary_fixtures_are_valid_container_shapes(self) -> None:
        fixture_root = PROJECT_ROOT / "fixtures/document-formats"
        with zipfile.ZipFile(fixture_root / "canonical.docx") as document:
            self.assertIn("word/document.xml", document.namelist())
        self.assertTrue(
            (fixture_root / "canonical.pdf").read_bytes().startswith(b"%PDF-")
        )

    def test_active_contract_binds_the_document_evaluation_suite(self) -> None:
        path = (
            PROJECT_ROOT
            / "skills/document-html-visualizer-skill/skill_contract.json"
        )
        contract = load_approved_skill_contract(path)
        case_ids = {
            load_task_case(case_path).task_case_id
            for case_path in (
                PROJECT_ROOT / "task-cases/document-formats"
            ).glob("*.json")
        }

        self.assertEqual(contract["status"], "approved")
        self.assertEqual(
            contract["evaluation"]["suite_refs"],
            ["document-html-visualizer-v2"],
        )
        self.assertEqual(len(case_ids), 5)


if __name__ == "__main__":
    unittest.main()
