"""Small dependency-free replay fixtures for trajectory viewer tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from skill_evolution.trajectory_user_report import build_trajectory_user_report


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _record(
    run_id: str,
    sequence: int,
    record_type: str,
    payload: Mapping[str, Any] | None = None,
    *,
    source: str = "framework",
) -> dict[str, Any]:
    return {
        "schema": "trajectory.actions.v1",
        "run_id": run_id,
        "seq": sequence,
        "observed_at": f"2026-07-25T00:00:{sequence:02d}+00:00",
        "elapsed_ms": sequence * 10,
        "source": source,
        "type": record_type,
        "payload": dict(payload or {}),
    }


def create_campaign(
    replays_root: Path,
    *,
    campaign_id: str = "campaign-1",
    run_specs: Sequence[Mapping[str, Any]] | None = None,
) -> Path:
    """Create a compact campaign with complete action-level trajectories."""

    specifications = list(
        run_specs
        or [
            {
                "run_id": "run-1",
                "status": "succeeded",
                "model_id": "model-a",
                "tool_status": "failed",
            }
        ]
    )
    campaign_directory = replays_root / campaign_id
    prompt_directory = campaign_directory / "prompt"
    prompt_directory.mkdir(parents=True)
    template = "Execute this approved skill: {{SKILL_CONTENT}}\n"
    rendered = "Execute this approved skill: fixture skill\n"
    (prompt_directory / "template.md").write_text(
        template,
        encoding="utf-8",
    )
    (prompt_directory / "rendered.md").write_text(
        rendered,
        encoding="utf-8",
    )
    _write_json(
        prompt_directory / "approval.json",
        {
            "status": "approved",
            "prompt_id": "fixture.viewer",
            "version": "1",
            "approved_by": "test-owner",
        },
    )

    run_records: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0
    for index, specification in enumerate(specifications, start=1):
        run_id = str(specification.get("run_id", f"run-{index}"))
        status = str(specification.get("status", "succeeded"))
        model_id = str(specification.get("model_id", "model-a"))
        tool_status = str(
            specification.get("tool_status", "succeeded")
        )
        thinking_level = str(
            specification.get("thinking_level", "off")
        )
        skill_content = str(
            specification.get("skill_content", "fixture skill\n")
        )
        input_content = str(
            specification.get("input_content", "fixture input\n")
        )
        run_directory = campaign_directory / "runs" / run_id
        skill_directory = run_directory / "artifacts" / "skill"
        skill_directory.mkdir(parents=True)
        (skill_directory / "SKILL.md").write_text(
            skill_content,
            encoding="utf-8",
        )
        (run_directory / "artifacts" / "input.md").write_text(
            input_content,
            encoding="utf-8",
        )
        (run_directory / "artifacts" / "output.html").write_text(
            (
                "<!doctype html><title>fixture</title>"
                "<h1>Fixture artifact</h1>"
                "<script>document.body.dataset.executed='yes'</script>"
            ),
            encoding="utf-8",
        )
        (run_directory / "pi-session.jsonl").write_text(
            '{"type":"session","id":"fixture"}\n',
            encoding="utf-8",
        )

        runtime = {
            "python": "3.11.0",
            "platform": "fixture-os",
            "working_directory": str(run_directory / "artifacts"),
            "pi_args": [
                "--tools",
                "read,write,bash",
                "--thinking",
                thinking_level,
            ],
        }
        trajectory_manifest = {
            "schema": "trajectory.actions.v1",
            "run_id": run_id,
            "task_case": {
                "prompt": rendered,
                "input": "artifacts/input.md",
                "expected_artifact": "artifacts/output.html",
            },
            "skill": {
                "snapshot_path": "artifacts/skill",
                "inventory": [
                    {
                        "path": "SKILL.md",
                        "bytes": len(skill_content.encode("utf-8")),
                    }
                ],
            },
            "source": {
                "path": "artifacts/input.md",
                "bytes": len(input_content.encode("utf-8")),
            },
            "runtime": runtime,
        }
        model = {
            "id": model_id,
            "name": model_id,
            "provider": "fixture",
        }
        records = [
            _record(
                run_id,
                1,
                "trajectory_started",
                {"manifest": trajectory_manifest},
            ),
            _record(
                run_id,
                2,
                "runtime_observed",
                {
                    "model": model,
                    "thinking_level": thinking_level,
                    "session_id": f"session-{run_id}",
                    "session_name": f"trajectory-{run_id}",
                },
            ),
            _record(
                run_id,
                3,
                "skill_resolved",
                {
                    "loaded": True,
                    "path": "artifacts/skill/SKILL.md",
                },
            ),
            _record(
                run_id,
                4,
                "pi_process_starting",
                {
                    "command": [
                        "pi",
                        "--tools",
                        "read,write,bash",
                        "--mode",
                        "rpc",
                    ]
                },
            ),
            _record(
                run_id,
                5,
                "turn_start",
                {"event_type": "turn_start"},
                source="pi_rpc",
            ),
            _record(
                run_id,
                6,
                "message_action",
                {
                    "status": "completed",
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": rendered}],
                    },
                },
                source="pi_rpc",
            ),
            _record(
                run_id,
                7,
                "message_action",
                {
                    "status": "completed",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Use the tool."},
                            {
                                "type": "toolCall",
                                "id": "call-1",
                                "name": "bash",
                                "arguments": {"command": "false"},
                            },
                        ],
                        "usage": {
                            "input": 100,
                            "output": 20,
                            "cacheRead": 50,
                            "cacheWrite": 0,
                            "totalTokens": 170,
                            "cost": {"total": 0.0125},
                        },
                    },
                },
                source="pi_rpc",
            ),
            _record(
                run_id,
                8,
                "tool_action",
                {
                    "tool_call_id": "call-1",
                    "tool_name": "bash",
                    "arguments": {"command": "false"},
                    "status": tool_status,
                    "result": {"content": [{"type": "text", "text": "done"}]},
                    "duration_ms": 12,
                },
                source="pi_rpc",
            ),
            _record(
                run_id,
                9,
                "message_action",
                {
                    "status": "completed",
                    "message": {
                        "role": "toolResult",
                        "toolCallId": "call-1",
                        "content": [{"type": "text", "text": "done"}],
                    },
                },
                source="pi_rpc",
            ),
            _record(
                run_id,
                10,
                "turn_end",
                {"event_type": "turn_end", "tool_result_count": 1},
                source="pi_rpc",
            ),
            _record(
                run_id,
                11,
                "trajectory_finished",
                {
                    "outcome": {
                        "status": status,
                        "duration_ms": 1250 + index,
                        "skill_loaded": True,
                        "session": {
                            "path": "pi-session.jsonl",
                            "status": "complete",
                            "message_count": 3,
                            "bytes": 34,
                        },
                        "artifact": {
                            "path": "artifacts/output.html",
                            "exists": True,
                            "bytes": 120,
                        },
                    }
                },
            ),
            _record(
                run_id,
                12,
                "trajectory_sealed",
                {"status": status, "record_count": 12},
            ),
        ]
        (run_directory / "trajectory.jsonl").write_text(
            "".join(
                json.dumps(record, ensure_ascii=False) + "\n"
                for record in records
            ),
            encoding="utf-8",
        )
        run_record = {
            "index": index,
            "status": status,
            "started_at": "2026-07-25T00:00:00+00:00",
            "ended_at": "2026-07-25T00:00:02+00:00",
            "duration_ms": 1250 + index,
            "run_id": run_id,
            "path": f"runs/{run_id}",
            "trajectory": f"runs/{run_id}/trajectory.jsonl",
            "session": f"runs/{run_id}/pi-session.jsonl",
            "session_status": "complete",
            "artifact": {
                "path": "artifacts/output.html",
                "exists": True,
                "bytes": 120,
            },
            "failure_stage": None if status == "succeeded" else "prompt",
            "error": None,
        }
        run_records.append(run_record)
        if status == "succeeded":
            succeeded += 1
        else:
            failed += 1

    manifest = {
        "schema": "replay.campaign.v1",
        "campaign_id": campaign_id,
        "status": "completed" if not failed else "completed_with_run_failures",
        "started_at": "2026-07-25T00:00:00+00:00",
        "ended_at": "2026-07-25T00:01:00+00:00",
        "duration_ms": 60000,
        "replay_count_requested": len(run_records),
        "skill": {"source_path": "/fixture/skill"},
        "task": {
            "source_path": "/fixture/input.md",
            "prompt": {
                "prompt_id": "fixture.viewer",
                "version": "1",
                "status": "approved",
                "approved_by": "test-owner",
                "template_snapshot": "prompt/template.md",
                "approval_snapshot": "prompt/approval.json",
                "rendered_snapshot": "prompt/rendered.md",
            },
        },
        "execution": {"mode": "sequential", "timeout_seconds": 30},
        "runs": run_records,
        "summary": {
            "trajectory_count": len(run_records),
            "succeeded": succeeded,
            "failed": failed,
            "orchestration_failed": 0,
        },
    }
    _write_json(campaign_directory / "replay.json", manifest)
    return campaign_directory


def create_trajectory_user_report(
    analyses_root: Path,
    *,
    run_id: str = "run-1",
    agent_run_id: str = "agent-run-1",
) -> Path:
    """Create a valid unavailable-state report for viewer tests."""

    precheck = {
        "schema": "trajectory.precheck.v1",
        "run_id": run_id,
        "deterministic_status": "completed_with_signals",
        "integrity": {"status": "valid"},
        "outcome": {"status": "succeeded"},
        "signals": [
            {
                "id": "signal-1",
                "facts": {
                    "status": "failed",
                    "tool_name": "bash",
                },
                "evidence": {
                    "schema": "evidence.ref.v1",
                    "run_id": run_id,
                    "seq": 8,
                },
            }
        ],
        "candidate_recoveries": [],
        "artifacts": [],
    }
    context = {
        "run_id": run_id,
        "trajectory_precheck_path": "reports/trajectory-precheck.json",
        "precheck_deterministic_status": "completed_with_signals",
        "precheck_integrity_status": "valid",
        "precheck_signal_ids": ["signal-1"],
    }
    report = build_trajectory_user_report(
        precheck=precheck,
        semantic_report=None,
        semantic_status="invalid_output",
        analysis_id="analysis-1",
        agent_run_id=agent_run_id,
        context=context,
        generated_at="2026-08-09T00:00:00+00:00",
    )
    destination = (
        analyses_root / "agent-runs" / agent_run_id / "user-report.json"
    )
    destination.parent.mkdir(parents=True)
    _write_json(destination, report)
    return destination
