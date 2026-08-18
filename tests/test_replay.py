"""Tests for approval-bound N-run replay campaign behavior."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.prompt_approval import (
    PromptApprovalError,
    approve_prompt,
    load_approved_prompt,
    render_execution_template,
)
from scripts.replay import run_replay_campaign
from scripts.task_case import TaskCase
from tests.test_trajectory_spike import (
    FAKE_PI,
    write_approved_skill_contract,
)


class ReplayCampaignTest(unittest.TestCase):
    def make_inputs(
        self,
        root: Path,
        *,
        approved: bool = True,
    ) -> tuple[Path, Path, Path]:
        skill = root / "skill"
        skill.mkdir()
        (skill / "SKILL.md").write_text(
            "---\nname: test\ndescription: test skill\n---\n",
            encoding="utf-8",
        )
        write_approved_skill_contract(skill)
        source = root / "source.md"
        source.write_text("# Input\n\nExample.", encoding="utf-8")
        prompt = root / "prompt-v1.md"
        prompt.write_text(
            (
                "Execute this skill:\n{{SKILL_CONTENT}}\n"
                "Task:\n{{TASK_CASE}}\n"
            ),
            encoding="utf-8",
        )
        approval = root / "prompt-v1.md.approval.json"
        approval.write_text(
            json.dumps(
                {
                    "schema": "prompt.approval.v1",
                    "status": "proposed",
                    "prompt_id": "test.replay",
                    "version": "1",
                    "prompt_file": prompt.name,
                    "content_sha256": None,
                    "approved_by": None,
                    "approved_at": None,
                }
            ),
            encoding="utf-8",
        )
        if approved:
            approve_prompt(prompt, approved_by="test-owner")
        return skill, source, prompt

    def run_fake_replay(
        self,
        root: Path,
        *,
        replay_count: int,
        extra_pi_args: list[str] | None = None,
    ):
        skill, source, prompt = self.make_inputs(root)
        return run_replay_campaign(
            skill_path=skill,
            source_path=source,
            prompt_path=prompt,
            replay_count=replay_count,
            output_root=root / "replays",
            timeout=2,
            pi_command=[sys.executable, "-u", "-c", FAKE_PI],
            extra_pi_args=extra_pi_args or [],
        )

    def test_replay_creates_n_trajectories_and_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            result = self.run_fake_replay(root, replay_count=3)
            manifest = result.manifest

            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["replay_count_requested"], 3)
            self.assertEqual(manifest["summary"]["trajectory_count"], 3)
            self.assertEqual(manifest["summary"]["succeeded"], 3)
            self.assertEqual(len(manifest["runs"]), 3)
            self.assertEqual(
                [run["index"] for run in manifest["runs"]],
                [1, 2, 3],
            )
            self.assertEqual(
                len({run["run_id"] for run in manifest["runs"]}),
                3,
            )

            for run in manifest["runs"]:
                run_directory = result.campaign_directory / run["path"]
                self.assertTrue(
                    (run_directory / "trajectory.jsonl").is_file()
                )
                self.assertTrue(
                    (run_directory / "pi-session.jsonl").is_file()
                )
                self.assertEqual(run["session_status"], "complete")

            template_snapshot = (
                result.campaign_directory / "prompt" / "template.md"
            )
            rendered_snapshot = (
                result.campaign_directory / "prompt" / "rendered.md"
            )
            self.assertTrue(template_snapshot.is_file())
            self.assertTrue(rendered_snapshot.is_file())
            self.assertTrue(
                (
                    result.campaign_directory
                    / "prompt"
                    / "approval.json"
                ).is_file()
            )
            rendered_text = rendered_snapshot.read_text(encoding="utf-8")
            self.assertIn("name: test", rendered_text)
            self.assertNotIn("{{SKILL_CONTENT}}", rendered_text)

            user_prompts = []
            for run in manifest["runs"]:
                trajectory_path = (
                    result.campaign_directory / run["trajectory"]
                )
                records = [
                    json.loads(line)
                    for line in trajectory_path.read_text().splitlines()
                ]
                user_message = next(
                    record["payload"]["message"]
                    for record in records
                    if record["type"] == "message_action"
                    and record["payload"]["message"]["role"] == "user"
                )
                user_prompts.append(user_message["content"])
            self.assertEqual(user_prompts, [rendered_text] * 3)
            saved = json.loads(
                (
                    result.campaign_directory / "replay.json"
                ).read_text()
            )
            self.assertEqual(saved, manifest)
            self.assertFalse(
                any(result.campaign_directory.rglob("*.tmp"))
            )

    def test_failed_runs_are_preserved_and_do_not_stop_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            result = self.run_fake_replay(
                root,
                replay_count=2,
                extra_pi_args=["--fake-fail"],
            )

            self.assertEqual(
                result.manifest["status"],
                "completed_with_run_failures",
            )
            self.assertEqual(
                result.manifest["summary"]["trajectory_count"],
                2,
            )
            self.assertEqual(result.manifest["summary"]["failed"], 2)
            for run in result.manifest["runs"]:
                self.assertEqual(run["status"], "failed")
                run_directory = result.campaign_directory / run["path"]
                self.assertTrue(
                    (run_directory / "trajectory.jsonl").is_file()
                )
                self.assertTrue(
                    (run_directory / "pi-session.jsonl").is_file()
                )

    def test_default_replay_creates_same_revision_execution_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill, source, prompt = self.make_inputs(root)

            result = run_replay_campaign(
                skill_path=skill,
                source_path=source,
                prompt_path=prompt,
                replay_count=2,
                runtime_root=root / "runtime",
                timeout=2,
                pi_command=[sys.executable, "-u", "-c", FAKE_PI],
            )

            self.assertEqual(result.manifest["schema"], "execution.set.v1")
            self.assertEqual(result.manifest["status"], "completed")
            self.assertEqual(len(result.manifest["execution_ids"]), 2)
            revision_ids = set()
            for execution_id in result.manifest["execution_ids"]:
                manifest = json.loads(
                    (
                        root
                        / "runtime"
                        / "skills"
                        / "test"
                        / "executions"
                        / execution_id
                        / "execution.json"
                    ).read_text()
                )
                revision_ids.add(manifest["revision_id"])
                self.assertEqual(manifest["origin"], "replay")
                self.assertEqual(
                    manifest["execution_set_id"], result.execution_set_id
                )
            self.assertEqual(revision_ids, {result.revision_id})

    def test_unapproved_prompt_is_rejected_before_campaign_creation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill, source, prompt = self.make_inputs(
                root,
                approved=False,
            )

            with self.assertRaisesRegex(
                PromptApprovalError,
                "not approved",
            ):
                run_replay_campaign(
                    skill_path=skill,
                    source_path=source,
                    prompt_path=prompt,
                    replay_count=2,
                    output_root=root / "replays",
                )

            self.assertFalse((root / "replays").exists())

    def test_unapproved_skill_contract_is_rejected_before_campaign_creation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill, source, prompt = self.make_inputs(root)
            contract_path = skill / "skill_contract.json"
            contract = json.loads(contract_path.read_text(encoding="utf-8"))
            contract.update(
                {
                    "status": "proposed",
                    "approved_by": None,
                    "approved_at": None,
                }
            )
            contract_path.write_text(
                json.dumps(contract),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "not approved",
            ):
                run_replay_campaign(
                    skill_path=skill,
                    source_path=source,
                    prompt_path=prompt,
                    replay_count=1,
                    output_root=root / "replays",
                )

            self.assertFalse((root / "replays").exists())

    def test_prompt_change_after_approval_requires_new_review(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, _, prompt = self.make_inputs(root)
            prompt.write_text(
                "Changed after owner approval.\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                PromptApprovalError,
                "changed after approval",
            ):
                load_approved_prompt(prompt)

    def test_skill_template_requires_exactly_one_placeholder(self) -> None:
        for name, template in (
            ("missing", "Execute the skill.\n"),
            (
                "duplicate",
                "{{SKILL_CONTENT}}\n{{SKILL_CONTENT}}\n",
            ),
        ):
            with self.subTest(name=name):
                with tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    skill, source, prompt = self.make_inputs(root)
                    prompt.write_text(template, encoding="utf-8")
                    approve_prompt(prompt, approved_by="test-owner")

                    with self.assertRaisesRegex(
                        PromptApprovalError,
                        "exactly once",
                    ):
                        run_replay_campaign(
                            skill_path=skill,
                            source_path=source,
                            prompt_path=prompt,
                            replay_count=1,
                            output_root=root / "replays",
                        )

                    self.assertFalse((root / "replays").exists())

    def test_execution_template_injects_structured_task_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill, source, _ = self.make_inputs(root)
            task_case = TaskCase.for_file(
                source,
                task_case_id="structured-task",
                expected_artifacts=["output.html", "summary.json"],
            )

            rendered = render_execution_template(
                (
                    "SKILL\n{{SKILL_CONTENT}}\n"
                    "TASK\n{{TASK_CASE}}\n"
                ),
                skill,
                task_case.prompt_payload(),
            )

            self.assertIn('"type": "file"', rendered.text)
            self.assertIn('"path": "input/source.md"', rendered.text)
            self.assertIn('"summary.json"', rendered.text)
            self.assertNotIn('"schema"', rendered.text)
            self.assertNotIn('"task_case_id"', rendered.text)
            self.assertNotIn('"capability_tags"', rendered.text)
            self.assertNotIn('"budget"', rendered.text)
            self.assertNotIn("{{TASK_CASE}}", rendered.text)
            self.assertNotIn("{{SKILL_CONTENT}}", rendered.text)

    def test_replay_accepts_inline_task_case_and_records_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill, _, prompt = self.make_inputs(root)
            prompt.write_text(
                (
                    "Execute this skill:\n{{SKILL_CONTENT}}\n"
                    "Task:\n{{TASK_CASE}}\n"
                ),
                encoding="utf-8",
            )
            approve_prompt(prompt, approved_by="test-owner")
            task_case = TaskCase.for_inline_text(
                "Pasted source.",
                task_case_id="inline-replay",
            )

            result = run_replay_campaign(
                skill_path=skill,
                task_case=task_case,
                prompt_path=prompt,
                replay_count=1,
                output_root=root / "replays",
                timeout=2,
                pi_command=[sys.executable, "-u", "-c", FAKE_PI],
            )

            self.assertEqual(result.manifest["status"], "completed")
            self.assertEqual(
                result.manifest["task"]["task_case"]["delivery"],
                "inline_text",
            )
            rendered = (
                result.campaign_directory / "prompt" / "rendered.md"
            ).read_text(encoding="utf-8")
            self.assertIn('"text": "Pasted source."', rendered)

    def test_execution_prompt_v2_is_proposed_not_executable(self) -> None:
        root = Path(__file__).resolve().parents[1]
        prompt = (
            root
            / "prompts"
            / "execution"
            / "document-html-visualizer-v2.md"
        )

        with self.assertRaisesRegex(PromptApprovalError, "not approved"):
            load_approved_prompt(prompt)

    def test_invalid_replay_count_is_rejected_before_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill, source, prompt = self.make_inputs(root)

            with self.assertRaisesRegex(
                ValueError,
                "greater than zero",
            ):
                run_replay_campaign(
                    skill_path=skill,
                    source_path=source,
                    prompt_path=prompt,
                    replay_count=0,
                    output_root=root / "replays",
                )

            self.assertFalse((root / "replays").exists())

    def test_orchestration_failure_is_recorded_for_every_attempt(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            skill, source, prompt = self.make_inputs(root)
            calls = 0

            def failing_runner(**_kwargs):
                nonlocal calls
                calls += 1
                raise RuntimeError("cannot create trajectory")

            result = run_replay_campaign(
                skill_path=skill,
                source_path=source,
                prompt_path=prompt,
                replay_count=2,
                output_root=root / "replays",
                trajectory_runner=failing_runner,
            )

            self.assertEqual(calls, 2)
            self.assertEqual(result.manifest["status"], "failed")
            self.assertEqual(
                result.manifest["summary"]["trajectory_count"],
                0,
            )
            self.assertEqual(
                result.manifest["summary"]["orchestration_failed"],
                2,
            )
            self.assertEqual(
                [run["status"] for run in result.manifest["runs"]],
                ["orchestration_failed", "orchestration_failed"],
            )

    def test_script_is_directly_invocable(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "replay.py"

        result = subprocess.run(
            [sys.executable, str(script), "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--prompt-file", result.stdout)
        self.assertIn("--replays", result.stdout)


if __name__ == "__main__":
    unittest.main()
