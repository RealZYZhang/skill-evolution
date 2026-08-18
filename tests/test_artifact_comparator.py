"""Tests for deterministic HTML artifact comparison."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts.artifact_comparator import (
    COMPARISON_SCHEMA,
    EVIDENCE_SCHEMA,
    HTMLArtifactComparator,
    _capture_viewport,
    _inject_screenshot_guards,
    _run_cli,
)
from skill_evolution.evidence import EvidenceRef
from tests.trajectory_viewer_fixtures import create_campaign


LEFT_HTML = """<!doctype html>
<html>
<head>
  <title>Alpha</title>
  <style>
    :root { --accent: #123456; }
    body { color: rgb(1, 2, 3); font-family: Inter, sans-serif; }
    @media (max-width: 600px) { body { color: #fff; } }
    .remote { background-image: url("https://cdn.example/bg.png"); }
  </style>
  <link rel="stylesheet" href="https://cdn.example/site.css">
</head>
<body>
  <header><nav><a href="#facts">Facts</a></nav></header>
  <main>
    <h1 id="title">Report 2026</h1>
    <section id="facts" class="card primary" data-component="facts">
      <h2>Facts</h2>
      <p aria-describedby="note">Budget 12% at https://example.test.</p>
      <p id="note">Approved.</p>
      <table><tr><th>Name</th><th>Value</th></tr>
      <tr><td>A</td><td>12%</td></tr></table>
      <details><summary>More</summary><p>Visible detail.</p></details>
    </section>
  </main>
  <script>document.body.dataset.ready = "yes";</script>
</body>
</html>
"""

RIGHT_HTML = """<!doctype html>
<html>
<head>
  <title>Beta</title>
  <style>:root { --brand: #abcdef; } body { font-family: serif; }</style>
</head>
<body>
  <main>
    <h1>Report 2026</h1>
    <article class="panel"><h2>Summary</h2><p>Budget 12%.</p></article>
    <a href="#missing">Missing</a>
    <div aria-labelledby="unknown">ARIA</div>
  </main>
</body>
</html>
"""

SOURCE_MD = """# Report 2026

## Facts

Budget 12% at https://example.test.

| Name | Value |
| --- | --- |
| A | 12% |
"""


class HTMLArtifactComparatorTest(unittest.TestCase):
    def test_collects_static_facts_and_evidence_references(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.md"
            left = root / "left.html"
            source.write_text(SOURCE_MD, encoding="utf-8")
            left.write_text(LEFT_HTML, encoding="utf-8")

            report = HTMLArtifactComparator().compare(
                {"run-left": left},
                source_path=source,
                campaign_id="campaign-1",
                run_ids={"run-left": "run-left"},
                display_paths={
                    "run-left": "runs/run-left/artifacts/output.html"
                },
            )

            self.assertEqual(report["schema"], COMPARISON_SCHEMA)
            self.assertEqual(report["status"], "complete")
            artifact = report["artifacts"][0]
            facts = artifact["facts"]
            self.assertEqual(facts["document"]["title"], "Alpha")
            self.assertEqual(facts["structure"]["tag_counts"]["section"], 1)
            self.assertEqual(facts["structure"]["tag_counts"]["details"], 1)
            self.assertEqual(
                facts["structure"]["tables"][0]["header_cells"],
                ["Name", "Value"],
            )
            self.assertEqual(
                facts["links"]["unresolved_local_anchors"],
                [],
            )
            self.assertEqual(
                facts["accessibility"]["unresolved_id_references"],
                [],
            )
            self.assertEqual(
                facts["dependencies"]["external_count"],
                2,
            )
            self.assertEqual(
                {item["name"] for item in facts["css"]["variables"]},
                {"--accent"},
            )
            self.assertIn(
                "Inter, sans-serif",
                {item["value"] for item in facts["css"]["fonts"]},
            )
            self.assertGreater(facts["scripts"]["inline_bytes"], 0)
            heading_ref = facts["structure"]["headings"][0]["ref"]
            self.assertEqual(heading_ref["html_line"], 16)
            self.assertEqual(heading_ref["selector"], "#title")
            artifact_ref = artifact["evidence_ref"]
            self.assertEqual(artifact_ref["schema"], EVIDENCE_SCHEMA)
            self.assertEqual(artifact_ref["campaign_id"], "campaign-1")
            self.assertEqual(artifact_ref["run_id"], "run-left")
            self.assertNotIn("report_pointer", artifact_ref)
            normalized_ref = EvidenceRef.from_dict(artifact_ref).to_dict()
            self.assertEqual(
                normalized_ref["artifact_path"],
                "runs/run-left/artifacts/output.html",
            )

    def test_records_markdown_preservation_without_quality_score(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "input.md"
            left = root / "left.html"
            source.write_text(SOURCE_MD, encoding="utf-8")
            left.write_text(LEFT_HTML, encoding="utf-8")

            report = HTMLArtifactComparator().compare(
                {"left": left},
                source_path=source,
            )

            preservation = report["artifacts"][0][
                "source_preservation"
            ]
            self.assertEqual(preservation["status"], "measured")
            self.assertEqual(
                preservation["headings"]["preserved_in_order"],
                2,
            )
            self.assertEqual(
                preservation["numbers"]["missing_occurrences"],
                {},
            )
            self.assertEqual(preservation["urls"]["missing"], [])
            self.assertTrue(
                preservation["tables"]["header_results"][0][
                    "header_cells_preserved"
                ]
            )
            serialized = json.dumps(report).lower()
            self.assertNotIn('"score"', serialized)
            self.assertNotIn('"best"', serialized)
            self.assertNotIn('"ranking"', serialized)
            self.assertNotIn("sha256", serialized)

    def test_pairwise_delta_reports_objective_differences(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            left = root / "left.html"
            right = root / "right.html"
            left.write_text(LEFT_HTML, encoding="utf-8")
            right.write_text(RIGHT_HTML, encoding="utf-8")

            report = HTMLArtifactComparator().compare(
                {"left": left, "right": right}
            )

            self.assertEqual(len(report["pairwise"]), 1)
            delta = report["pairwise"][0]["delta"]
            self.assertEqual(
                delta["structure"]["tag_count_changes"]["section"],
                {"left": 1, "right": 0, "delta": -1},
            )
            self.assertEqual(
                delta["structure"]["tag_count_changes"]["article"],
                {"left": 0, "right": 1, "delta": 1},
            )
            self.assertIn(
                "--accent=#123456",
                delta["css"]["variables"]["only_left"],
            )
            self.assertIn(
                "--brand=#abcdef",
                delta["css"]["variables"]["only_right"],
            )
            right_facts = report["artifacts"][1]["facts"]
            self.assertEqual(
                len(right_facts["links"]["unresolved_local_anchors"]),
                1,
            )
            self.assertEqual(
                len(
                    right_facts["accessibility"][
                        "unresolved_id_references"
                    ]
                ),
                1,
            )
            self.assertEqual(
                len(report["pairwise"][0]["evidence_refs"]),
                2,
            )

    def test_missing_chrome_is_partial_and_source_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "artifact.html"
            screenshots = root / "screenshots"
            artifact.write_text(LEFT_HTML, encoding="utf-8")
            original = artifact.read_bytes()

            report = HTMLArtifactComparator(
                chrome_command=str(root / "missing-chrome")
            ).compare(
                {"artifact": artifact},
                capture_screenshots=True,
                screenshot_directory=screenshots,
            )

            self.assertEqual(report["status"], "partial")
            self.assertEqual(
                report["issues"][0]["code"],
                "chrome_not_found",
            )
            self.assertEqual(artifact.read_bytes(), original)
            self.assertEqual(report["artifacts"][0]["screenshots"], {})

    def test_screenshot_copy_blocks_network_and_preserves_inline_css(
        self,
    ) -> None:
        guarded = _inject_screenshot_guards(LEFT_HTML)

        self.assertIn("default-src 'none'", guarded)
        self.assertIn("connect-src 'none'", guarded)
        self.assertIn("style-src 'unsafe-inline'", guarded)
        self.assertIn("skill-evolution-probe", guarded)
        self.assertIn(LEFT_HTML.split("<head>", 1)[1], guarded)

    def test_capture_records_both_fixed_viewports(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "artifact.html"
            screenshots = root / "screenshots"
            chrome = root / "chrome"
            artifact.write_text(LEFT_HTML, encoding="utf-8")
            chrome.write_text("", encoding="utf-8")

            def fake_capture(
                *,
                command: list[str],
                destination: Path,
                timeout: float,
            ) -> tuple[bool, None]:
                self.assertGreater(timeout, 0)
                self.assertTrue(
                    any(
                        value.startswith("--screenshot=")
                        for value in command
                    )
                )
                destination.write_bytes(b"png")
                return True, None

            with mock.patch(
                "scripts.artifact_comparator._capture_viewport",
                side_effect=fake_capture,
            ):
                report = HTMLArtifactComparator(
                    chrome_command=str(chrome)
                ).compare(
                    {"artifact": artifact},
                    capture_screenshots=True,
                    screenshot_directory=screenshots,
                )

            captures = report["artifacts"][0]["screenshots"]
            self.assertEqual(report["status"], "complete")
            self.assertEqual(
                (captures["desktop"]["width"], captures["desktop"]["height"]),
                (1440, 900),
            )
            self.assertEqual(
                (captures["mobile"]["width"], captures["mobile"]["height"]),
                (390, 844),
            )
            self.assertFalse(Path(captures["desktop"]["path"]).is_absolute())
            self.assertFalse(Path(captures["mobile"]["path"]).is_absolute())
            self.assertTrue((root / captures["desktop"]["path"]).is_file())
            self.assertTrue((root / captures["mobile"]["path"]).is_file())

    def test_viewport_capture_stops_chrome_after_complete_png(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "capture.png"

            class FakeProcess:
                def __init__(self) -> None:
                    self.terminated = False

                def poll(self) -> None:
                    return None

                def terminate(self) -> None:
                    self.terminated = True

                def wait(self, timeout: float | None = None) -> int:
                    self.assert_timeout(timeout)
                    return 0

                def kill(self) -> None:
                    raise AssertionError("kill should not be required")

                def assert_timeout(
                    self,
                    timeout: float | None,
                ) -> None:
                    if timeout != 2.0:
                        raise AssertionError("unexpected wait timeout")

            process = FakeProcess()

            def fake_popen(
                _: list[str],
                **__: object,
            ) -> FakeProcess:
                destination.write_bytes(
                    b"\x89PNG\r\n\x1a\n"
                    b"x"
                    b"\x00\x00\x00\x00IEND\xaeB`\x82"
                )
                return process

            with mock.patch(
                "scripts.artifact_comparator.subprocess.Popen",
                side_effect=fake_popen,
            ):
                captured, failure = _capture_viewport(
                    command=["chrome"],
                    destination=destination,
                    timeout=1.0,
                )

            self.assertTrue(captured)
            self.assertIsNone(failure)
            self.assertTrue(process.terminated)

    def test_campaign_mode_uses_relative_paths_and_rejects_symlink_escape(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = create_campaign(
                root / "replays",
                run_specs=[
                    {"run_id": "run-1"},
                    {"run_id": "run-2"},
                ],
            )
            report = HTMLArtifactComparator().compare_campaign(campaign)

            self.assertEqual(report["status"], "complete")
            self.assertEqual(len(report["artifacts"]), 2)
            self.assertEqual(len(report["pairwise"]), 1)
            self.assertEqual(
                report["artifacts"][0]["artifact_path"],
                "runs/run-1/artifacts/output.html",
            )

            outside = root / "outside.html"
            outside.write_text(RIGHT_HTML, encoding="utf-8")
            escaped = (
                campaign
                / "runs"
                / "run-2"
                / "artifacts"
                / "output.html"
            )
            escaped.unlink()
            escaped.symlink_to(outside)

            partial = HTMLArtifactComparator().compare_campaign(campaign)

            self.assertEqual(partial["status"], "partial")
            self.assertEqual(len(partial["artifacts"]), 1)
            self.assertIn(
                "artifact_outside_campaign",
                {issue["code"] for issue in partial["issues"]},
            )

    def test_campaign_uses_preserved_taskcase_markdown_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = create_campaign(root / "replays")
            run_directory = campaign / "runs" / "run-1" / "artifacts"
            legacy_input = run_directory / "input.md"
            input_directory = run_directory / "input"
            input_directory.mkdir()
            (input_directory / "source.md").write_text(
                SOURCE_MD,
                encoding="utf-8",
            )
            legacy_input.unlink()
            manifest_path = campaign / "replay.json"
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            manifest["task"]["source_path"] = str(root / "missing.md")
            manifest_path.write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )

            report = HTMLArtifactComparator().compare_campaign(campaign)

            self.assertEqual(report["source"]["status"], "extracted")
            self.assertEqual(
                [heading["text"] for heading in report["source"]["headings"]],
                ["Report 2026", "Facts"],
            )

    def test_multiple_artifacts_compare_only_matching_html_roles(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = create_campaign(
                root / "replays",
                run_specs=[
                    {"run_id": "run-1"},
                    {"run_id": "run-2"},
                ],
            )
            manifest_path = campaign / "replay.json"
            manifest = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
            for run in manifest["runs"]:
                artifact_directory = (
                    campaign / run["path"] / "artifacts"
                )
                (artifact_directory / "alternate.html").write_text(
                    RIGHT_HTML,
                    encoding="utf-8",
                )
                (artifact_directory / "summary.json").write_text(
                    "{}",
                    encoding="utf-8",
                )
                run["artifacts"] = [
                    run["artifact"],
                    {
                        "path": "artifacts/alternate.html",
                        "exists": True,
                    },
                    {
                        "path": "artifacts/summary.json",
                        "exists": True,
                    },
                ]
            manifest_path.write_text(
                json.dumps(manifest),
                encoding="utf-8",
            )

            report = HTMLArtifactComparator().compare_campaign(campaign)

            self.assertEqual(report["status"], "complete")
            self.assertEqual(len(report["artifacts"]), 4)
            self.assertEqual(len(report["pairwise"]), 2)
            self.assertEqual(
                {
                    artifact["comparison_group"]
                    for artifact in report["artifacts"]
                },
                {"output.html", "alternate.html"},
            )
            self.assertTrue(
                all(
                    pair["left_artifact_id"].split(":", 1)[1]
                    == pair["right_artifact_id"].split(":", 1)[1]
                    for pair in report["pairwise"]
                )
            )
            self.assertEqual(
                sum(
                    issue["code"] == "artifact_not_html"
                    for issue in report["issues"]
                ),
                2,
            )

    def test_cli_writes_v1_report_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = create_campaign(root / "replays")
            output = root / "harness" / "artifact-comparison.json"

            with mock.patch("builtins.print"):
                status = _run_cli(
                    [
                        "--campaign",
                        str(campaign),
                        "--output",
                        str(output),
                    ]
                )

            self.assertEqual(status, 0)
            self.assertTrue(output.is_file())
            self.assertFalse(output.with_name(output.name + ".tmp").exists())
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["schema"], COMPARISON_SCHEMA)


if __name__ == "__main__":
    unittest.main()
