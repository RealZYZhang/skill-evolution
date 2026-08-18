"""Tests for the validated, non-secret root project configuration."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from skill_evolution.config import (
    CONFIG_SCHEMA,
    ProjectConfigurationError,
    default_config_path,
    load_project_configuration,
)
from skill_evolution.agents import ModelConfiguration
from skill_evolution.pi_runtime import PiAgentRuntime


class ProjectConfigurationTests(unittest.TestCase):
    """The checked-in configuration remains the runtime's safe default."""

    def test_checked_in_configuration_declares_default_pi_model(self) -> None:
        configuration = load_project_configuration()

        self.assertEqual(default_config_path().name, "config.yaml")
        self.assertEqual(configuration.pi_agent.provider, "deepseek")
        self.assertEqual(configuration.pi_agent.model, "deepseek-v4-pro")
        self.assertEqual(configuration.pi_agent.thinking, "off")
        self.assertEqual(
            ModelConfiguration.from_project_configuration().to_dict(),
            {
                "provider": "deepseek",
                "model": "deepseek-v4-pro",
                "thinking": "off",
            },
        )

    def test_pi_runtime_uses_checked_in_configuration_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = PiAgentRuntime(
                agent_runs_root=root / "agent-runs",
                extension_path=root / "root-jail.ts",
            )

        self.assertEqual(runtime.model.model, "deepseek-v4-pro")

    def test_unknown_settings_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        f"schema: {CONFIG_SCHEMA}",
                        "pi_agent:",
                        "  provider: deepseek",
                        "  model: deepseek-v4-pro",
                        "  thinking: off",
                        "credentials: forbidden",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ProjectConfigurationError,
                "Unsupported root configuration keys",
            ):
                load_project_configuration(path)

    def test_missing_model_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        f"schema: {CONFIG_SCHEMA}",
                        "pi_agent:",
                        "  provider: deepseek",
                        "  thinking: off",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ProjectConfigurationError,
                "pi_agent.model",
            ):
                load_project_configuration(path)


if __name__ == "__main__":
    unittest.main()
