"""Tests for analysis schemas, state transitions, and evidence-loop gates."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from skill_evolution.analysis import (
    AnalysisCampaignRepository,
    AnalysisContractError,
    ExperimentRequestRepository,
    load_approved_capability_contract,
    validate_agent_result,
    validate_capability_contract,
    validate_experiment_request,
    validate_optimization_hypothesis,
)
from skill_evolution.storage import StorageError
from skill_evolution.workflows import EvidenceLoopCoordinator


def _harness_request() -> dict[str, object]:
    return {
        "schema": "experiment.request.v1",
        "request_type": "harness_measurement",
        "hypothesis": "The current harness omitted an existing fact.",
        "why_existing_evidence_is_insufficient": (
            "The trajectory contains actions but no derived retry count."
        ),
        "supports_if": "The additional measurement finds retries.",
        "refutes_if": "The additional measurement finds no retries.",
        "measurements": ["retry_count"],
    }


def _replay_request() -> dict[str, object]:
    return {
        "schema": "experiment.request.v1",
        "request_type": "replay_experiment",
        "hypothesis": "TXT delivery follows a different execution path.",
        "why_existing_evidence_is_insufficient": (
            "The frozen batch only contains Markdown file delivery."
        ),
        "supports_if": "TXT runs introduce a repeatable additional branch.",
        "refutes_if": "TXT and Markdown use the same stable branch.",
        "changed_variables": ["input_format"],
        "held_constant_variables": ["content", "skill_version", "runtime"],
        "task_case_ids": ["same-content-txt"],
        "formats": [".txt"],
        "skill_version": "visualizer-v1",
        "runtime": {"provider": "deepseek", "model": "deepseek-v4-pro"},
        "replay_count": 3,
        "required_harnesses": [
            "trajectory.profile.v1",
            "artifact.comparison.v1",
        ],
        "budget": {"max_runs": 3},
    }


class AnalysisSchemaTest(unittest.TestCase):
    def test_legacy_existing_trajectory_request_is_normalized(self) -> None:
        request = validate_experiment_request(
            {
                "schema": "experiment.request.v1",
                "request_type": "existing_trajectory",
                "hypothesis": "One saved run may contain the missing branch.",
                "why_existing_evidence_is_insufficient": "The run was not selected.",
                "supports_if": "The branch is present.",
                "refutes_if": "The branch is absent.",
                "selection": "Select run-1.",
            }
        )

        self.assertEqual(request["request_type"], "existing_trajectory")

    def test_validates_capability_agent_and_hypothesis_contracts(self) -> None:
        capability = validate_capability_contract(
            {
                "schema": "skill.capability_contract.v1",
                "skill_id": "document-visualizer",
                "version": "v1",
                "status": "approved",
                "approved_by": "owner",
                "approved_at": "2026-07-26T00:00:00Z",
                "capabilities": [
                    {
                        "id": "markdown-file",
                        "claim": "Render Markdown supplied as a file.",
                        "delivery_modes": ["file"],
                        "formats": [".md"],
                        "required_evidence": ["artifact", "trajectory"],
                    }
                ],
            }
        )
        result = validate_agent_result(
            {
                "schema": "analysis.agent_result.v1",
                "role": "resource_efficiency_analyst",
                "findings": [
                    {
                        "id": "finding-1",
                        "claim": "A failed write caused rework.",
                        "confidence": 0.75,
                        "evidence": [
                            {
                                "schema": "evidence.ref.v1",
                                "run_id": "run-1",
                                "seq": 7,
                            }
                        ],
                        "counterevidence": [],
                        "optimization_point": "Bound the first write size.",
                    }
                ],
                "evidence_requests": [],
                "optimization_hypotheses": [],
            }
        )
        hypothesis = validate_optimization_hypothesis(
            {
                "schema": "optimization.hypothesis.v1",
                "id": "hypothesis-1",
                "problem": "The first complete write repeatedly fails.",
                "proposed_change": "Require chunked writes above a fixed size.",
                "expected_effect": "Reduce failed writes and rework.",
                "protected_dimensions": ["content_preservation"],
                "evidence": [
                    {
                        "schema": "evidence.ref.v1",
                        "run_id": "run-1",
                        "seq": 7,
                    }
                ],
            }
        )

        self.assertEqual(capability["status"], "approved")
        self.assertEqual(result["findings"][0]["confidence"], 0.75)
        self.assertTrue(hypothesis["atomic"])

    def test_rejects_duplicate_capabilities_and_unsubstantiated_findings(
        self,
    ) -> None:
        duplicated = {
            "schema": "skill.capability_contract.v1",
            "skill_id": "skill",
            "version": "v1",
            "status": "proposed",
            "capabilities": [
                {
                    "id": "same",
                    "claim": "First",
                    "delivery_modes": ["file"],
                    "formats": [".md"],
                    "required_evidence": ["artifact"],
                },
                {
                    "id": "same",
                    "claim": "Second",
                    "delivery_modes": ["inline_text"],
                    "formats": [".txt"],
                    "required_evidence": ["trajectory"],
                },
            ],
        }
        with self.assertRaisesRegex(
            AnalysisContractError,
            "Duplicate capability",
        ):
            validate_capability_contract(duplicated)

        with self.assertRaisesRegex(
            AnalysisContractError,
            "at least one evidence",
        ):
            validate_agent_result(
                {
                    "schema": "analysis.agent_result.v1",
                    "role": "analyst",
                    "findings": [
                        {
                            "id": "unsupported",
                            "claim": "Unsupported claim",
                            "confidence": 1,
                            "evidence": [],
                        }
                    ],
                }
            )

    def test_capability_contract_requires_explicit_approval_metadata(
        self,
    ) -> None:
        contract = {
            "schema": "skill.capability_contract.v1",
            "skill_id": "skill",
            "version": "v1",
            "status": "approved",
            "approved_by": None,
            "approved_at": None,
            "capabilities": [
                {
                    "id": "markdown",
                    "claim": "Render Markdown.",
                    "delivery_modes": ["file"],
                    "formats": ["md"],
                    "required_evidence": ["artifact"],
                }
            ],
        }
        with self.assertRaisesRegex(
            AnalysisContractError,
            "approved_by",
        ):
            validate_capability_contract(contract)

        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "contract.json"
            contract["status"] = "proposed"
            path.write_text(json.dumps(contract), encoding="utf-8")
            with self.assertRaisesRegex(
                AnalysisContractError,
                "not approved",
            ):
                load_approved_capability_contract(path)

    def test_replay_request_requires_controlled_variables_and_budget(
        self,
    ) -> None:
        normalized = validate_experiment_request(_replay_request())

        self.assertEqual(normalized["priority"], 3)
        self.assertEqual(normalized["replay_count"], 3)
        invalid = _replay_request()
        invalid["held_constant_variables"] = []
        with self.assertRaisesRegex(
            AnalysisContractError,
            "held_constant_variables must not be empty",
        ):
            validate_experiment_request(invalid)


class AnalysisStateTest(unittest.TestCase):
    def _repositories(
        self,
        root: Path,
    ) -> tuple[AnalysisCampaignRepository, ExperimentRequestRepository]:
        return (
            AnalysisCampaignRepository(root / "analyses", max_rounds=3),
            ExperimentRequestRepository(root / "requests"),
        )

    def _create_campaign(
        self,
        campaigns: AnalysisCampaignRepository,
    ) -> dict[str, object]:
        return campaigns.create(
            replay_campaign_id="replay-1",
            evidence_bundle="evidence/bundle.json",
            harness_versions={
                "profiler": "trajectory.profile.v1",
                "comparator": "artifact.comparison.v1",
            },
        )

    def _finish_round_needing_evidence(
        self,
        campaigns: AnalysisCampaignRepository,
        campaign_id: str,
        request_id: str,
    ) -> dict[str, object]:
        campaigns.record_specialists(
            campaign_id,
            ["specialist-1", "specialist-2", "specialist-3"],
        )
        return campaigns.finish_round(
            campaign_id,
            synthesis_run_id="synthesis-1",
            synthesis_result={
                "schema": "analysis.agent_result.v1",
                "role": "synthesis_agent",
            },
            request_ids=[request_id],
        )

    def test_state_transitions_preserve_all_round_attempts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaigns, _ = self._repositories(root)
            campaign = self._create_campaign(campaigns)
            campaign_id = str(campaign["id"])

            started = campaigns.start_round(campaign_id)
            self.assertEqual(started["status"], "analyzing")
            awaiting = self._finish_round_needing_evidence(
                campaigns,
                campaign_id,
                "request-1",
            )
            self.assertEqual(awaiting["status"], "awaiting_evidence")
            ready = campaigns.mark_evidence_ready(
                campaign_id,
                evidence_refs=[
                    {
                        "schema": "evidence.ref.v1",
                        "report_path": "reports/new.json",
                    }
                ],
            )
            self.assertEqual(ready["status"], "evidence_ready")
            second = campaigns.start_round(campaign_id)
            self.assertEqual(len(second["rounds"]), 2)
            self.assertEqual(second["rounds"][0]["status"], "evidence_ready")
            self.assertEqual(second["rounds"][1]["status"], "running")

            with self.assertRaisesRegex(StorageError, "cannot start"):
                campaigns.start_round(campaign_id)

    def test_proposed_request_cannot_produce_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaigns, requests = self._repositories(root)
            campaign = self._create_campaign(campaigns)
            campaign_id = str(campaign["id"])
            campaigns.start_round(campaign_id)
            request = requests.create(
                campaign_id=campaign_id,
                round_number=1,
                request=_harness_request(),
            )
            self._finish_round_needing_evidence(
                campaigns,
                campaign_id,
                str(request["id"]),
            )
            calls: list[str] = []
            coordinator = EvidenceLoopCoordinator(
                campaigns=campaigns,
                requests=requests,
            )

            with self.assertRaisesRegex(StorageError, "explicit.*approval"):
                coordinator.fulfil_approved(
                    str(request["id"]),
                    produce_evidence=lambda _: calls.append("produce") or [],
                    run_next_round=lambda _: calls.append("next"),
                )

            self.assertEqual(calls, [])
            self.assertEqual(
                requests.repository.load(str(request["id"]))["status"],
                "proposed",
            )

    def test_approved_request_chains_to_a_fresh_round_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaigns, requests = self._repositories(root)
            campaign = self._create_campaign(campaigns)
            campaign_id = str(campaign["id"])
            campaigns.start_round(campaign_id)
            request = requests.create(
                campaign_id=campaign_id,
                round_number=1,
                request=_harness_request(),
            )
            request_id = str(request["id"])
            self._finish_round_needing_evidence(
                campaigns,
                campaign_id,
                request_id,
            )
            requests.approve(request_id, approved_by="project-owner")
            produced: list[str] = []
            next_rounds: list[str] = []
            coordinator = EvidenceLoopCoordinator(
                campaigns=campaigns,
                requests=requests,
            )

            completed = coordinator.fulfil_approved(
                request_id,
                produce_evidence=lambda _: (
                    produced.append(request_id)
                    or [
                        {
                            "schema": "evidence.ref.v1",
                            "report_path": "reports/new-measurement.json",
                        }
                    ]
                ),
                run_next_round=lambda next_campaign_id: (
                    next_rounds.append(next_campaign_id),
                    campaigns.start_round(next_campaign_id),
                ),
            )

            self.assertEqual(completed["status"], "completed")
            self.assertEqual(produced, [request_id])
            self.assertEqual(next_rounds, [campaign_id])
            updated = campaigns.repository.load(campaign_id)
            self.assertEqual(updated["status"], "analyzing")
            self.assertEqual(len(updated["rounds"]), 2)
            with self.assertRaisesRegex(StorageError, "approval"):
                coordinator.fulfil_approved(
                    request_id,
                    produce_evidence=lambda _: [],
                    run_next_round=lambda _: None,
                )
            self.assertEqual(produced, [request_id])

    def test_third_inconclusive_round_cannot_trigger_more_sampling(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            campaigns, requests = self._repositories(root)
            campaign = self._create_campaign(campaigns)
            campaign_id = str(campaign["id"])
            coordinator = EvidenceLoopCoordinator(
                campaigns=campaigns,
                requests=requests,
            )
            third_request_id = ""

            for round_number in range(1, 4):
                if round_number == 1:
                    campaigns.start_round(campaign_id)
                request = requests.create(
                    campaign_id=campaign_id,
                    round_number=round_number,
                    request=_harness_request(),
                )
                request_id = str(request["id"])
                final = self._finish_round_needing_evidence(
                    campaigns,
                    campaign_id,
                    request_id,
                )
                if round_number < 3:
                    requests.approve(
                        request_id,
                        approved_by="project-owner",
                    )
                    coordinator.fulfil_approved(
                        request_id,
                        produce_evidence=lambda _: [
                            {
                                "schema": "evidence.ref.v1",
                                "report_path": "reports/new.json",
                            }
                        ],
                        run_next_round=lambda next_campaign_id: (
                            campaigns.start_round(next_campaign_id)
                        ),
                    )
                else:
                    third_request_id = request_id

            self.assertEqual(final["status"], "inconclusive")
            self.assertEqual(len(final["rounds"]), 3)
            requests.approve(
                third_request_id,
                approved_by="project-owner",
            )
            calls: list[str] = []
            with self.assertRaisesRegex(StorageError, "inconclusive"):
                coordinator.fulfil_approved(
                    third_request_id,
                    produce_evidence=lambda _: calls.append("produce") or [
                        {"schema": "evidence.ref.v1", "report_path": "new.json"}
                    ],
                    run_next_round=lambda _: calls.append("next"),
                )
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
