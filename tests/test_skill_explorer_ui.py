"""Tests for the Chinese-first Skill Explorer information hierarchy."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SkillExplorerUiTests(unittest.TestCase):
    """Keep the owner-approved presentation rules visible in source."""

    def test_execution_cards_keep_prompt_in_a_separate_disclosure(self) -> None:
        source = (ROOT / "web" / "trajectory-viewer" / "app.js").read_text(
            encoding="utf-8"
        )

        task_text = source[source.index("function taskText"):]
        task_text = task_text[: task_text.index("function baseName")]
        self.assertNotIn("task.prompt", task_text)
        self.assertIn('node("details", "prompt-disclosure")', source)
        self.assertIn('"展开任务 prompt"', source)
        self.assertIn('data-detail-tab="prompt"', (
            ROOT / "web" / "trajectory-viewer" / "index.html"
        ).read_text(encoding="utf-8"))

    def test_sidebar_is_a_skill_trajectory_analysis_tree(self) -> None:
        source = (ROOT / "web" / "trajectory-viewer" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertIn('node("ul", "trajectory-menu")', source)
        self.assertIn('node("button", "trajectory-menu-item")', source)
        self.assertIn('node("button", "trajectory-analysis-item"', source)

    def test_execution_detail_is_progressive_and_trajectory_can_show_all(self) -> None:
        source = (ROOT / "web" / "trajectory-viewer" / "app.js").read_text(
            encoding="utf-8"
        )

        self.assertNotIn('"查看记录信息"', source)
        self.assertIn('state.trajectoryMode === "all"', source)
        self.assertIn(
            '"关键步骤由固定规则在页面读取时选出，不使用 LLM；原始 trajectory 没有被删改。"',
            source,
        )
        self.assertIn('node("details", `trajectory-value', source)
        self.assertNotIn("jsonBlock(step.payload)", source)
        self.assertIn('"网页预览"', source)
        self.assertIn('"基础检查已完成"', source)
        self.assertIn('"语义分析未通过质量检查"', source)

    def test_visible_statuses_and_specialist_terms_have_chinese_help(self) -> None:
        javascript = (ROOT / "web" / "trajectory-viewer" / "app.js").read_text(
            encoding="utf-8"
        )
        html = (ROOT / "web" / "trajectory-viewer" / "index.html").read_text(
            encoding="utf-8"
        )

        self.assertIn('succeeded: "成功"', javascript)
        self.assertIn('failed: "失败"', javascript)
        self.assertIn('high: "证据充分"', javascript)
        self.assertIn('yes: "是"', javascript)
        self.assertIn('statusLabel(incident.skill_change)', javascript)
        self.assertIn('statusLabel(status)', javascript)
        self.assertIn('class="help-mark"', html)
        self.assertIn('data-tooltip=', html)
        self.assertNotIn(">Executions<", html)
        self.assertNotIn(">Improvements<", html)
        self.assertNotIn(">Revision<", html)


if __name__ == "__main__":
    unittest.main()
