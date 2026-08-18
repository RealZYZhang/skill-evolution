"""Approval-bound inventory tests for every production prompt."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest

from scripts.prompt_approval import (
    PromptApprovalError,
    load_approved_prompt,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ProductionPromptInventoryTests(unittest.TestCase):
    """Unreviewed prompts must remain visible but impossible to execute."""

    def test_analysis_prompts_have_current_approval_states(self) -> None:
        prompts = sorted((PROJECT_ROOT / "prompts/analysis").glob("*.md"))

        self.assertEqual(len(prompts), 15)
        for prompt in prompts:
            with self.subTest(prompt=prompt.name):
                sidecar = prompt.with_name(prompt.name + ".approval.json")
                approval = json.loads(sidecar.read_text(encoding="utf-8"))
                self.assertEqual(approval["schema"], "prompt.approval.v1")
                self.assertEqual(approval["prompt_file"], prompt.name)
                if prompt.name in {
                    "trajectory-error-analysis-v1.md",
                    "trajectory-error-analysis-v2.md",
                    "behavior-pattern-research-v1.md",
                    "conditions-coverage-research-v1.md",
                    "outcome-consistency-research-v1.md",
                    "resource-efficiency-research-v1.md",
                    "error-identification-v1.md",
                    "error-analyst-v1.md",
                }:
                    self.assertEqual(approval["status"], "approved")
                    self.assertIsInstance(approval["content_sha256"], str)
                    load_approved_prompt(prompt)
                else:
                    self.assertEqual(approval["status"], "proposed")
                    self.assertIsNone(approval["content_sha256"])
                    with self.assertRaises(PromptApprovalError):
                        load_approved_prompt(prompt)

    def test_research_prompts_are_approved_and_keep_answers_out(self) -> None:
        filenames = {
            "behavior-pattern-research-v1.md": "BehaviorPatternAnalyst",
            "conditions-coverage-research-v1.md": (
                "ConditionsCoverageAnalyst"
            ),
            "outcome-consistency-research-v1.md": (
                "OutcomeConsistencyAnalyst"
            ),
            "resource-efficiency-research-v1.md": (
                "ResourceEfficiencyAnalyst"
            ),
        }
        for filename, role in filenames.items():
            with self.subTest(prompt=filename):
                path = PROJECT_ROOT / "prompts/analysis" / filename
                text = path.read_text(encoding="utf-8")
                approval = json.loads(
                    path.with_name(path.name + ".approval.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(approval["status"], "approved")
                self.assertIsInstance(approval["content_sha256"], str)
                load_approved_prompt(path)
                self.assertIn(role, text)
                self.assertIn("submit_multi_trajectory_research", text)
                self.assertIn("不可信研究数据", text)
                self.assertNotIn("generate.py", text)
                self.assertNotIn("gen_html.py", text)
                if filename == "behavior-pattern-research-v1.md":
                    for leaked_category in (
                        "临时脚本",
                        "验证器",
                        "绕路",
                        "相似脚本",
                    ):
                        self.assertNotIn(leaked_category, text)

    def test_research_harness_context_binds_current_extensions(self) -> None:
        context_path = (
            PROJECT_ROOT
            / "prompts/analysis/research-harness-context-v1.json"
        )
        context = json.loads(context_path.read_text(encoding="utf-8"))
        bindings = {
            item["name"]: item
            for item in context["prompt_visible_extensions"]
        }
        expected = {
            "research_tools": "research-tools.ts",
            "research_output": "research-output.ts",
        }

        self.assertEqual(set(bindings), set(expected))
        for name, filename in expected.items():
            with self.subTest(extension=name):
                source = PROJECT_ROOT / "extensions" / filename
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                self.assertEqual(bindings[name]["file"], filename)
                self.assertEqual(bindings[name]["sha256"], digest)

    def test_trajectory_error_prompt_covers_semantic_states_and_uses_precheck(
        self,
    ) -> None:
        prompt = (
            PROJECT_ROOT / "prompts/analysis/trajectory-error-analysis-v2.md"
        ).read_text(encoding="utf-8")

        for state in (
            "no_observed_error",
            "errors_recovered",
            "terminal_failure",
            "incomplete_or_indeterminate",
            "invalid_or_inconsistent",
            "insufficient_evidence",
        ):
            with self.subTest(state=state):
                self.assertIn(state, prompt)
        for semantic_judgment in (
            "Signal 含义",
            "恢复是否成立",
            "因果关系",
            "责任边界",
            "语义完成度",
            "Skill 修复适用性",
        ):
            with self.subTest(semantic_judgment=semantic_judgment):
                self.assertIn(semantic_judgment, prompt)
        self.assertIn("trajectory.precheck.v1", prompt)
        self.assertIn("trajectory_precheck_path", prompt)
        self.assertIn("不要完整扫描 trajectory", prompt)
        self.assertIn("candidate_recoveries", prompt)
        self.assertIn("不证明失败影响已消除", prompt)
        self.assertIn("analysis.trajectory_error_report.v1", prompt)
        self.assertIn("expected_control_flow", prompt)
        self.assertIn("report_path + json_pointer", prompt)

    def test_trajectory_error_v2_is_json_only_and_shows_valid_evidence_shapes(
        self,
    ) -> None:
        prompt_path = (
            PROJECT_ROOT / "prompts/analysis/trajectory-error-analysis-v2.md"
        )
        prompt = prompt_path.read_text(encoding="utf-8")
        approval = json.loads(
            prompt_path.with_name(
                prompt_path.name + ".approval.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(approval["status"], "approved")
        self.assertIsInstance(approval["content_sha256"], str)
        self.assertIn("第一个非空白字符必须是 `{`", prompt)
        self.assertIn("禁止 Markdown 代码围栏", prompt)
        self.assertIn('"report_path"', prompt)
        self.assertIn('"json_pointer"', prompt)
        self.assertIn('"run_id"', prompt)
        self.assertIn('"seq"', prompt)
        self.assertIn('"artifact_path"', prompt)
        self.assertIn('"line"', prompt)
        self.assertIn('"selector"', prompt)
        self.assertIn("禁止 incident 指向自身", prompt)
        load_approved_prompt(prompt_path)

    def test_trajectory_error_v3_is_proposed_and_requires_chinese_narrative(
        self,
    ) -> None:
        prompt_path = (
            PROJECT_ROOT / "prompts/analysis/trajectory-error-analysis-v3.md"
        )
        prompt = prompt_path.read_text(encoding="utf-8")
        approval = json.loads(
            prompt_path.with_name(
                prompt_path.name + ".approval.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(approval["status"], "proposed")
        self.assertIsNone(approval["content_sha256"])
        self.assertIn(
            "面向用户的所有自然语言结论必须使用简体中文",
            prompt,
        )
        self.assertIn("不得输出完整英文句子或英文段落", prompt)
        with self.assertRaises(PromptApprovalError):
            load_approved_prompt(prompt_path)

    def test_execution_v2_is_proposed_with_exact_placeholders(self) -> None:
        prompt = (
            PROJECT_ROOT
            / "prompts/execution/document-html-visualizer-v2.md"
        )
        text = prompt.read_text(encoding="utf-8")
        approval = json.loads(
            prompt.with_name(prompt.name + ".approval.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(text.count("{{SKILL_CONTENT}}"), 1)
        self.assertEqual(text.count("{{TASK_CASE}}"), 1)
        self.assertEqual(approval["status"], "proposed")
        with self.assertRaises(PromptApprovalError):
            load_approved_prompt(prompt)

    def test_execution_v2_contains_only_generic_execution_requirements(
        self,
    ) -> None:
        prompt = (
            PROJECT_ROOT
            / "prompts/execution/document-html-visualizer-v2.md"
        )
        text = prompt.read_text(encoding="utf-8")

        for skill_specific_text in (
            "可视化策略",
            "保留源文档",
            "HTML 的基本结构",
            "本地锚点",
            "外部依赖",
        ):
            with self.subTest(text=skill_specific_text):
                self.assertNotIn(skill_specific_text, text)

        self.assertIn("完整遵守 skill 中定义的执行流程", text)
        self.assertIn("`input.type` 为 `file`", text)
        self.assertIn("逐一检查每个预期产物", text)


if __name__ == "__main__":
    unittest.main()
