"""Tests for confined evidence references and frozen redacted bundles."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from skill_evolution.evidence import (
    ComparisonEvidenceBundleBuilder,
    EVIDENCE_BUNDLE_SCHEMA,
    EvidenceBundleBuilder,
    EvidenceError,
    EvidenceRef,
    resolve_inside,
    sanitize_for_evidence,
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class EvidenceReferenceTest(unittest.TestCase):
    def _make_bundle(self, root: Path) -> Path:
        bundle = root / "bundle"
        trajectory = bundle / "runs" / "run-1" / "trajectory.jsonl"
        trajectory.parent.mkdir(parents=True)
        trajectory.write_text(
            "\n".join(
                (
                    json.dumps({"seq": 1, "type": "message_action"}),
                    json.dumps({"seq": 2, "type": "tool_action"}),
                )
            )
            + "\n",
            encoding="utf-8",
        )
        _write_json(
            bundle / "reports" / "profile.json",
            {"runs": [{"run_id": "run-1"}]},
        )
        artifact = bundle / "runs" / "run-1" / "artifacts" / "output.html"
        artifact.parent.mkdir()
        artifact.write_text("<h1>Title</h1>\n<p>Body</p>\n", encoding="utf-8")
        return bundle

    def test_validates_trajectory_report_and_artifact_locations(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self._make_bundle(Path(temporary))

            EvidenceRef(run_id="run-1", seq=2).validate(bundle)
            EvidenceRef(
                report_path="reports/profile.json",
                json_pointer="/runs/0/run_id",
            ).validate(bundle)
            EvidenceRef(
                artifact_path="runs/run-1/artifacts/output.html",
                line=2,
                selector="p",
            ).validate(bundle)

    def test_rejects_a_missing_trajectory_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self._make_bundle(Path(temporary))

            with self.assertRaisesRegex(EvidenceError, "seq 99"):
                EvidenceRef(run_id="run-1", seq=99).validate(bundle)

    def test_legacy_trajectory_filename_remains_referenceable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            bundle = self._make_bundle(Path(temporary))
            current = bundle / "runs/run-1/trajectory.jsonl"
            legacy = bundle / "runs/run-1/trajectory.jsonl"
            current.rename(legacy)

            EvidenceRef(run_id="run-1", seq=2).validate(bundle)

    def test_rejects_path_traversal_and_absolute_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "inside.txt").write_text("inside", encoding="utf-8")

            for unsafe in ("../outside.txt", "/tmp/outside.txt"):
                with self.subTest(path=unsafe):
                    with self.assertRaisesRegex(EvidenceError, "escapes"):
                        resolve_inside(root, unsafe)
                    with self.assertRaisesRegex(EvidenceError, "escapes"):
                        EvidenceRef(report_path=unsafe).validate(root)

    def test_rejects_a_symlink_that_resolves_outside_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            bundle = root / "bundle"
            bundle.mkdir()
            outside = root / "outside.html"
            outside.write_text("<p>outside</p>", encoding="utf-8")
            link = bundle / "linked.html"
            link.symlink_to(outside)

            with self.assertRaisesRegex(EvidenceError, "escapes"):
                EvidenceRef(artifact_path="linked.html").validate(bundle)

    def test_serialized_reference_requires_a_real_locator(self) -> None:
        with self.assertRaisesRegex(EvidenceError, "no locator"):
            EvidenceRef.from_dict({"schema": "evidence.ref.v1"})
        with self.assertRaisesRegex(EvidenceError, "requires run_id"):
            EvidenceRef.from_dict(
                {"schema": "evidence.ref.v1", "seq": 1}
            )


class EvidenceRedactionTest(unittest.TestCase):
    def _make_campaign(self, root: Path) -> Path:
        campaign = root / "campaign"
        run = campaign / "runs" / "run-a"
        run.mkdir(parents=True)
        _write_json(
            campaign / "replay.json",
            {"schema": "replay.campaign.v1", "campaign_id": "campaign-1"},
        )
        (run / "trajectory.jsonl").write_text(
            '{"seq":1,"type":"message_action"}\n',
            encoding="utf-8",
        )
        return campaign

    def test_redacts_credentials_environment_and_hidden_reasoning(self) -> None:
        sanitized = sanitize_for_evidence(
            {
                "authorization": "Bearer private",
                "nested": {
                    "api-key": "private",
                    "environment": {"TOKEN": "private"},
                },
                "content": [
                    {"type": "thinking", "text": "private reasoning"},
                    {"type": "text", "text": "observable result"},
                ],
            }
        )

        self.assertEqual(sanitized["authorization"], "[REDACTED]")
        self.assertEqual(sanitized["nested"]["api-key"], "[REDACTED]")
        self.assertEqual(
            sanitized["nested"]["environment"],
            "[REDACTED_ENVIRONMENT]",
        )
        self.assertEqual(
            sanitized["content"][0],
            {
                "type": "thinking",
                "redacted": "[HIDDEN_MODEL_REASONING]",
            },
        )
        self.assertEqual(
            sanitized["content"][1]["text"],
            "observable result",
        )

    def test_bundle_keeps_observable_actions_but_omits_pi_session(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = root / "campaign"
            run = campaign / "runs" / "run-a"
            artifacts = run / "artifacts"
            artifacts.mkdir(parents=True)
            _write_json(
                campaign / "replay.json",
                {"schema": "replay.campaign.v1", "campaign_id": "campaign-1"},
            )
            trajectory_records = [
                {
                    "seq": 1,
                    "type": "message_action",
                    "payload": {
                        "content": [
                            {"type": "thinking", "text": "hidden"},
                            {"type": "text", "text": "visible"},
                        ]
                    },
                },
                {
                    "seq": 2,
                    "type": "tool_action",
                    "payload": {
                        "tool_name": "write",
                        "arguments": {"path": "output.html"},
                        "api_key": "private",
                    },
                },
            ]
            (run / "trajectory.jsonl").write_text(
                "".join(
                    json.dumps(record, ensure_ascii=False) + "\n"
                    for record in trajectory_records
                ),
                encoding="utf-8",
            )
            (run / "pi-session.jsonl").write_text(
                '{"secret":"session-only"}\n',
                encoding="utf-8",
            )
            (artifacts / "output.html").write_text(
                "<h1>Visible artifact</h1>\n",
                encoding="utf-8",
            )
            profile = root / "profile.json"
            comparison = root / "comparison.json"
            _write_json(profile, {"schema": "trajectory.profile.v1"})
            _write_json(comparison, {"schema": "artifact.comparison.v1"})
            destination = root / "evidence"

            built = EvidenceBundleBuilder().build(
                campaign_directory=campaign,
                destination=destination,
                profile_path=profile,
                comparison_path=comparison,
            )

            manifest = json.loads(
                (built / "bundle.json").read_text(encoding="utf-8")
            )
            copied = [
                json.loads(line)
                for line in (
                    built / "runs" / "run-a" / "trajectory.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(manifest["schema"], EVIDENCE_BUNDLE_SCHEMA)
            self.assertEqual(manifest["runs"][0]["trajectory_records"], 2)
            self.assertEqual(
                copied[0]["payload"]["content"][0]["redacted"],
                "[HIDDEN_MODEL_REASONING]",
            )
            self.assertEqual(
                copied[0]["payload"]["content"][1]["text"],
                "visible",
            )
            self.assertEqual(
                copied[1]["payload"]["api_key"],
                "[REDACTED]",
            )
            self.assertTrue(
                (
                    built
                    / "runs"
                    / "run-a"
                    / "artifacts"
                    / "output.html"
                ).is_file()
            )
            self.assertFalse(
                (built / "runs" / "run-a" / "pi-session.jsonl").exists()
            )

    def test_bundle_rejects_a_non_empty_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "evidence"
            destination.mkdir()
            (destination / "existing").write_text("keep", encoding="utf-8")

            with self.assertRaisesRegex(EvidenceError, "not empty"):
                EvidenceBundleBuilder().build(
                    campaign_directory=root,
                    destination=destination,
                    profile_path=root / "missing-profile.json",
                    comparison_path=root / "missing-comparison.json",
                )

    def test_bundle_archives_only_captured_comparator_screenshots(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = self._make_campaign(root)
            harness = root / "harness"
            screenshots = harness / "screenshots"
            screenshots.mkdir(parents=True)
            (screenshots / "desktop.png").write_bytes(b"desktop-png")
            profile = harness / "profile.json"
            comparison = harness / "comparison.json"
            _write_json(profile, {"schema": "trajectory.profile.v1"})
            _write_json(
                comparison,
                {
                    "schema": "artifact.comparison.v1",
                    "status": "partial",
                    "artifacts": [
                        {
                            "artifact_id": "artifact",
                            "screenshots": {
                                "desktop": {
                                    "status": "captured",
                                    "path": "screenshots/desktop.png",
                                    "bytes": 1,
                                },
                                "mobile": {
                                    "status": "failed",
                                    "path": "screenshots/mobile.png",
                                },
                            },
                        }
                    ],
                },
            )

            built = EvidenceBundleBuilder().build(
                campaign_directory=campaign,
                destination=root / "evidence",
                profile_path=profile,
                comparison_path=comparison,
            )

            manifest = json.loads(
                (built / "bundle.json").read_text(encoding="utf-8")
            )
            archived = json.loads(
                (
                    built / "reports" / "artifact-comparison.json"
                ).read_text(encoding="utf-8")
            )
            archived_screenshots = archived["artifacts"][0]["screenshots"]
            desktop_path = archived_screenshots["desktop"]["path"]
            self.assertEqual(manifest["screenshots"], [desktop_path])
            self.assertEqual(
                (built / desktop_path).read_bytes(),
                b"desktop-png",
            )
            self.assertEqual(
                archived_screenshots["desktop"]["bytes"],
                len(b"desktop-png"),
            )
            self.assertEqual(
                archived_screenshots["mobile"]["status"],
                "failed",
            )
            self.assertNotIn("path", archived_screenshots["mobile"])
            self.assertEqual(
                archived_screenshots["mobile"]["attempted_path"],
                "screenshots/mobile.png",
            )
            self.assertFalse((built / "screenshots" / "mobile.png").exists())

    def test_bundle_rejects_screenshot_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = self._make_campaign(root)
            harness = root / "harness"
            harness.mkdir()
            profile = harness / "profile.json"
            comparison = harness / "comparison.json"
            _write_json(profile, {"schema": "trajectory.profile.v1"})
            _write_json(
                comparison,
                {
                    "schema": "artifact.comparison.v1",
                    "artifacts": [
                        {
                            "screenshots": {
                                "desktop": {
                                    "status": "captured",
                                    "path": "../outside.png",
                                }
                            }
                        }
                    ],
                },
            )
            (root / "outside.png").write_bytes(b"outside")

            with self.assertRaisesRegex(EvidenceError, "escapes"):
                EvidenceBundleBuilder().build(
                    campaign_directory=campaign,
                    destination=root / "evidence",
                    profile_path=profile,
                    comparison_path=comparison,
                )

    def test_bundle_rejects_screenshot_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = self._make_campaign(root)
            harness = root / "harness"
            screenshots = harness / "screenshots"
            screenshots.mkdir(parents=True)
            outside = root / "outside.png"
            outside.write_bytes(b"outside")
            (screenshots / "linked.png").symlink_to(outside)
            profile = harness / "profile.json"
            comparison = harness / "comparison.json"
            _write_json(profile, {"schema": "trajectory.profile.v1"})
            _write_json(
                comparison,
                {
                    "schema": "artifact.comparison.v1",
                    "artifacts": [
                        {
                            "screenshots": {
                                "desktop": {
                                    "status": "captured",
                                    "path": "screenshots/linked.png",
                                }
                            }
                        }
                    ],
                },
            )

            with self.assertRaisesRegex(EvidenceError, "escapes"):
                EvidenceBundleBuilder().build(
                    campaign_directory=campaign,
                    destination=root / "evidence",
                    profile_path=profile,
                    comparison_path=comparison,
                )

    def test_judge_bundle_adds_comparison_candidate_and_diff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaign = self._make_campaign(root)
            harness = root / "harness"
            harness.mkdir()
            _write_json(
                harness / "trajectory-profile.json",
                {"schema": "trajectory.profile.v1"},
            )
            _write_json(
                harness / "artifact-comparison.json",
                {
                    "schema": "artifact.comparison.v1",
                    "artifacts": [],
                },
            )
            comparison = root / "comparison"
            candidate = root / "candidate"
            comparison.mkdir()
            candidate.mkdir()
            _write_json(
                comparison / "manifest.json",
                {
                    "schema": "comparison.experiment.v1",
                    "id": "comparison-1",
                },
            )
            _write_json(
                candidate / "manifest.json",
                {
                    "schema": "candidate.skill.v1",
                    "id": "candidate-1",
                },
            )
            (candidate / "diff.patch").write_text(
                "--- a/SKILL.md\n+++ b/SKILL.md\n",
                encoding="utf-8",
            )
            contract = root / "contract.json"
            _write_json(
                contract,
                {"schema": "skill.capability_contract.v1"},
            )

            built = ComparisonEvidenceBundleBuilder().build(
                comparison_directory=comparison,
                batch_campaign_directory=campaign,
                harness_directory=harness,
                candidate_directory=candidate,
                destination=root / "judge-evidence",
                skill_contract_path=contract,
            )

            manifest = json.loads(
                (built / "bundle.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["purpose"], "replay_judge")
            self.assertTrue((built / manifest["comparison"]).is_file())
            self.assertTrue(
                (built / manifest["candidate_manifest"]).is_file()
            )
            self.assertEqual(
                (built / manifest["candidate_diff"]).read_text(
                    encoding="utf-8"
                ),
                "--- a/SKILL.md\n+++ b/SKILL.md\n",
            )


if __name__ == "__main__":
    unittest.main()
