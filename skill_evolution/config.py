"""Load the repository's validated, non-secret project configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


CONFIG_SCHEMA = "skill-evolution.config.v1"


class ProjectConfigurationError(ValueError):
    """Raised when the root project configuration is missing or invalid."""


@dataclass(frozen=True)
class PiAgentSettings:
    """Non-secret Pi model settings shared by production runtimes."""

    provider: str
    model: str
    thinking: str


@dataclass(frozen=True)
class ProjectConfiguration:
    """Validated settings stored in the root ``config.yaml`` file."""

    pi_agent: PiAgentSettings


def project_root() -> Path:
    """Return the repository root that owns the canonical configuration."""

    return Path(__file__).resolve().parents[1]


def default_config_path() -> Path:
    """Return the canonical root-level configuration path."""

    return project_root() / "config.yaml"


def load_project_configuration(
    path: str | Path | None = None,
) -> ProjectConfiguration:
    """Load the limited YAML schema used for non-secret project defaults.

    The MVP intentionally supports only the mapping-and-scalar YAML needed by
    ``config.yaml``. It avoids adding a parser dependency while rejecting
    features that the project configuration does not use.
    """

    config_path = Path(path) if path is not None else default_config_path()
    try:
        content = config_path.read_text(encoding="utf-8")
    except OSError as error:
        raise ProjectConfigurationError(
            f"Cannot read project configuration: {config_path}"
        ) from error
    values = _parse_limited_yaml(content, config_path)
    schema = values.get("schema")
    if schema != CONFIG_SCHEMA:
        raise ProjectConfigurationError(
            f"config.yaml schema must be {CONFIG_SCHEMA!r}"
        )
    allowed_root = {"schema", "pi_agent"}
    unexpected_root = set(values) - allowed_root
    if unexpected_root:
        raise ProjectConfigurationError(
            "Unsupported root configuration keys: "
            + ", ".join(sorted(unexpected_root))
        )
    pi_agent = values.get("pi_agent")
    if not isinstance(pi_agent, dict):
        raise ProjectConfigurationError("config.yaml requires pi_agent mapping")
    allowed_pi_agent = {"provider", "model", "thinking"}
    unexpected_pi_agent = set(pi_agent) - allowed_pi_agent
    if unexpected_pi_agent:
        raise ProjectConfigurationError(
            "Unsupported pi_agent configuration keys: "
            + ", ".join(sorted(unexpected_pi_agent))
        )
    required = {
        key: _required_string(pi_agent, key)
        for key in sorted(allowed_pi_agent)
    }
    return ProjectConfiguration(
        pi_agent=PiAgentSettings(
            provider=required["provider"],
            model=required["model"],
            thinking=required["thinking"],
        )
    )


def _required_string(values: dict[str, str], key: str) -> str:
    """Return one required non-empty scalar from a parsed mapping."""

    value = values.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProjectConfigurationError(
            f"config.yaml requires non-empty pi_agent.{key}"
        )
    return value


def _parse_limited_yaml(
    content: str,
    path: Path,
) -> dict[str, str | dict[str, str]]:
    """Parse the two-level scalar mapping schema accepted by ``config.yaml``."""

    parsed: dict[str, str | dict[str, str]] = {}
    current_mapping: dict[str, str] | None = None
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        indentation = len(raw_line) - len(raw_line.lstrip(" "))
        if "\t" in raw_line[: len(raw_line) - len(raw_line.lstrip())]:
            raise _yaml_error(path, line_number, "tabs are not supported")
        if indentation == 0:
            key, separator, value = line.partition(":")
            if not separator or not key:
                raise _yaml_error(path, line_number, "expected a root key")
            if key in parsed:
                raise _yaml_error(path, line_number, f"duplicate key {key!r}")
            if value.strip():
                parsed[key] = _scalar(value.strip(), path, line_number)
                current_mapping = None
            else:
                mapping: dict[str, str] = {}
                parsed[key] = mapping
                current_mapping = mapping
            continue
        if indentation != 2 or current_mapping is None:
            raise _yaml_error(
                path,
                line_number,
                "expected a two-space nested mapping entry",
            )
        key, separator, value = line.partition(":")
        if not separator or not key or not value.strip():
            raise _yaml_error(path, line_number, "expected a scalar mapping entry")
        if key in current_mapping:
            raise _yaml_error(path, line_number, f"duplicate key {key!r}")
        current_mapping[key] = _scalar(value.strip(), path, line_number)
    return parsed


def _scalar(value: str, path: Path, line_number: int) -> str:
    """Decode one deliberately simple YAML scalar."""

    if value.startswith(("'", '"')):
        quote = value[0]
        if len(value) < 2 or not value.endswith(quote):
            raise _yaml_error(path, line_number, "unterminated quoted scalar")
        return value[1:-1]
    if " #" in value or ":" in value:
        raise _yaml_error(path, line_number, "unsupported scalar syntax")
    return value


def _yaml_error(
    path: Path,
    line_number: int,
    message: str,
) -> ProjectConfigurationError:
    """Build a source-located configuration validation error."""

    return ProjectConfigurationError(f"{path}:{line_number}: {message}")
