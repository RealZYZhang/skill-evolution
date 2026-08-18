"""Tests for versioned TaskCase validation and serialization."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.task_case import TaskCase, load_task_case


class TaskCaseTest(unittest.TestCase):
    def test_loads_file_fixture_relative_to_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixtures = root / "fixtures"
            fixtures.mkdir()
            source = fixtures / "source.docx"
            source.write_bytes(b"fake-docx")
            definition = root / "task.json"
            definition.write_text(
                json.dumps(
                    {
                        "schema": "task.case.v1",
                        "task_case_id": "docx-basic",
                        "delivery": "file",
                        "input": {"path": "fixtures/source.docx"},
                        "expected_artifacts": [
                            "output.html",
                            "reports/validation.json",
                        ],
                        "capability_tags": ["format:docx"],
                        "budget": {"timeout_seconds": 30},
                    }
                ),
                encoding="utf-8",
            )

            task_case = load_task_case(definition)

            self.assertEqual(task_case.source_path, source.resolve())
            self.assertEqual(task_case.source_name, "source.docx")
            self.assertEqual(
                task_case.expected_artifacts,
                ("output.html", "reports/validation.json"),
            )
            self.assertEqual(
                task_case.prompt_payload()["input"]["path"],
                "input/source.docx",
            )
            self.assertEqual(
                set(task_case.prompt_payload()),
                {"input", "expected_artifacts"},
            )
            self.assertEqual(
                task_case.prompt_payload()["input"]["type"],
                "file",
            )
            self.assertEqual(
                task_case.record_payload()["capability_tags"],
                ["format:docx"],
            )
            self.assertEqual(
                task_case.record_payload()["budget"],
                {"timeout_seconds": 30},
            )

    def test_loads_inline_text_without_creating_a_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            definition = root / "task.json"
            definition.write_text(
                json.dumps(
                    {
                        "schema": "task.case.v1",
                        "task_case_id": "inline-basic",
                        "delivery": "inline_text",
                        "input": {"text": "Pasted text"},
                        "expected_artifacts": ["output.html"],
                    }
                ),
                encoding="utf-8",
            )

            task_case = load_task_case(definition)

            self.assertEqual(task_case.delivery, "inline_text")
            self.assertIsNone(task_case.source_path)
            self.assertEqual(
                task_case.prompt_payload()["input"],
                {"type": "inline_text", "text": "Pasted text"},
            )

    def test_rejects_unsafe_or_duplicate_artifact_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.md"
            source.write_text("input", encoding="utf-8")

            for expected, message in (
                (["../outside.html"], "stay inside"),
                (["/outside.html"], "stay inside"),
                (["input/source.md"], "reserved"),
                (["output.html", "output.html"], "duplicates"),
            ):
                with self.subTest(expected=expected):
                    with self.assertRaisesRegex(ValueError, message):
                        TaskCase.for_file(
                            source,
                            expected_artifacts=expected,
                        )

    def test_rejects_mixed_or_empty_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.md"
            source.write_text("input", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "cannot include inline"):
                TaskCase(
                    task_case_id="mixed",
                    delivery="file",
                    source_path=source,
                    inline_text="also inline",
                )
            with self.assertRaisesRegex(ValueError, "must be a non-empty"):
                TaskCase.for_inline_text(
                    "",
                    task_case_id="empty-inline",
                )


if __name__ == "__main__":
    unittest.main()
