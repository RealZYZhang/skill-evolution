"""Boundary tests for the accepted package-local Skill Contract v2."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from skill_evolution.analysis import (
    AnalysisContractError,
    load_approved_skill_contract,
    validate_capability_contract,
    validate_skill_contract_document,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = PROJECT_ROOT / "contracts/schemas/skill-contract-v2.schema.json"
SKILL_DIRECTORY = PROJECT_ROOT / "skills/document-html-visualizer-skill"
CONTRACT_PATH = SKILL_DIRECTORY / "skill_contract.json"
LEGACY_PATH = (
    PROJECT_ROOT / "contracts/skills/document-html-visualizer-v1.json"
)


def _load(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise AssertionError(f"Expected JSON object: {path}")
    return value


class SkillContractV2Tests(unittest.TestCase):
    """Keep the active contract strict, general, and version-extensible."""

    def test_checked_in_contract_is_approved_and_package_local(self) -> None:
        schema = _load(SCHEMA_PATH)
        contract = load_approved_skill_contract(CONTRACT_PATH)

        self.assertEqual(
            schema["$id"],
            "urn:skill-evolution:schema:skill-contract:v2",
        )
        self.assertEqual(
            schema["properties"]["schema"]["const"],
            "skill.contract.v2",
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(contract))
        self.assertEqual(CONTRACT_PATH.name, "skill_contract.json")
        self.assertEqual(contract["status"], "approved")

    def test_runtime_boundary_is_machine_checkable_and_fail_closed(
        self,
    ) -> None:
        contract = load_approved_skill_contract(CONTRACT_PATH)
        runtime = contract["runtime"]

        self.assertLessEqual(
            set(runtime["required_tools"]),
            set(runtime["allowed_tools"]),
        )
        self.assertEqual(runtime["network"], "forbidden")
        self.assertFalse(runtime["credentials_in_sandbox"])

    def test_semantics_are_absent_and_evaluation_is_versioned(
        self,
    ) -> None:
        contract = _load(CONTRACT_PATH)

        self.assertNotIn("semantics", contract)
        self.assertEqual(set(contract["evaluation"]), {"suite_refs"})
        extended = deepcopy(contract)
        extended["evaluation"]["metrics"] = ["duration_ms"]
        with self.assertRaisesRegex(
            AnalysisContractError,
            "unexpected=.*metrics",
        ):
            validate_skill_contract_document(extended)

    def test_invalid_runtime_and_filename_are_rejected(self) -> None:
        contract = _load(CONTRACT_PATH)
        contract["runtime"]["required_tools"].append("network.fetch")
        with self.assertRaisesRegex(AnalysisContractError, "subset"):
            validate_skill_contract_document(contract)

        with tempfile.TemporaryDirectory() as temporary:
            wrong_name = Path(temporary) / "contract.json"
            wrong_name.write_text(
                CONTRACT_PATH.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                AnalysisContractError,
                "skill_contract.json",
            ):
                load_approved_skill_contract(wrong_name)

    def test_legacy_v1_remains_readable_but_is_not_the_active_contract(
        self,
    ) -> None:
        legacy = validate_capability_contract(_load(LEGACY_PATH))
        active = load_approved_skill_contract(CONTRACT_PATH)

        self.assertEqual(legacy["schema"], "skill.capability_contract.v1")
        self.assertEqual(legacy["status"], "proposed")
        self.assertEqual(active["schema"], "skill.contract.v2")
        self.assertEqual(
            active["supersedes"]["schema"],
            legacy["schema"],
        )


if __name__ == "__main__":
    unittest.main()
