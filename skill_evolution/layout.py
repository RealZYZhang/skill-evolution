"""Canonical file-only project runtime layout."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class RuntimeLayout:
    """Paths for every independently inspectable workflow object."""

    root: Path

    @classmethod
    def from_root(
        cls,
        root: str | os.PathLike[str] = ".skill-evolution",
    ) -> RuntimeLayout:
        """Resolve a runtime root without creating it."""

        return cls(Path(root).resolve())

    @property
    def replays(self) -> Path:
        """Return the legacy replay root used only before hierarchy cutover."""

        return self.root / "replays"

    @property
    def skills(self) -> Path:
        """Return the canonical Skill-first aggregate root."""

        return self.root / "skills"

    @property
    def migrations(self) -> Path:
        """Return the auditable hierarchy-migration journal root."""

        return self.root / "migrations"

    @property
    def catalog(self) -> Path:
        """Return the rebuildable Skill navigation catalog path."""

        return self.root / "catalog.json"

    @property
    def harness_runs(self) -> Path:
        return self.root / "harness-runs"

    @property
    def analyses(self) -> Path:
        return self.root / "analyses"

    @property
    def agent_runs(self) -> Path:
        return self.analyses / "agent-runs"

    @property
    def experiment_requests(self) -> Path:
        return self.root / "experiment-requests"

    @property
    def candidates(self) -> Path:
        return self.root / "candidates"

    @property
    def comparisons(self) -> Path:
        return self.root / "comparisons"

    @property
    def reviews(self) -> Path:
        return self.root / "reviews"

    def ensure(self) -> None:
        """Create only canonical Skill-first runtime roots."""

        for directory in (
            self.skills,
            self.migrations,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def ensure_legacy(self) -> None:
        """Create deprecated roots for explicit compatibility workflows."""

        for directory in (
            self.replays,
            self.harness_runs,
            self.analyses,
            self.agent_runs,
            self.experiment_requests,
            self.candidates,
            self.comparisons,
            self.reviews,
        ):
            directory.mkdir(parents=True, exist_ok=True)
