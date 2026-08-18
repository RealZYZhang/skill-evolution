/**
 * Strict structured-output tool for one-trajectory semantic error analysis.
 *
 * The model must submit its final report through this terminating tool. Pi
 * validates the complete argument object before execution; the Python runtime
 * then applies the trajectory-specific semantic and evidence checks.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";

const nonEmptyString = Type.String({ minLength: 1 });
const nullableString = Type.Union([nonEmptyString, Type.Null()]);

const evidenceReference = Type.Object(
  {
    schema: StringEnum(["evidence.ref.v1"] as const),
    campaign_id: Type.Optional(nonEmptyString),
    run_id: Type.Optional(nonEmptyString),
    seq: Type.Optional(Type.Integer({ minimum: 1 })),
    report_path: Type.Optional(nonEmptyString),
    json_pointer: Type.Optional(Type.String()),
    artifact_path: Type.Optional(nonEmptyString),
    line: Type.Optional(Type.Integer({ minimum: 1 })),
    selector: Type.Optional(nonEmptyString),
  },
  { additionalProperties: false },
);

const evidenceList = Type.Array(evidenceReference, { minItems: 1 });
const optionalEvidenceList = Type.Array(evidenceReference);
const stringList = Type.Array(nonEmptyString);

const incident = Type.Object(
  {
    id: nonEmptyString,
    source_signal_ids: stringList,
    disposition: StringEnum(
      [
        "terminal",
        "recovered",
        "expected_control_flow",
        "latent",
        "capture_integrity",
      ] as const,
    ),
    causal_role: StringEnum(
      [
        "root_cause",
        "contributing_cause",
        "symptom",
        "unrelated",
        "unknown",
      ] as const,
    ),
    attributed_to: StringEnum(
      [
        "skill",
        "task_or_input",
        "runtime_or_environment",
        "tool_or_dependency",
        "model_or_provider",
        "framework_or_capture",
        "harness",
        "unknown",
      ] as const,
    ),
    phase: nullableString,
    claim: nonEmptyString,
    confidence: Type.Number({ minimum: 0, maximum: 1 }),
    evidence: evidenceList,
    counterevidence: optionalEvidenceList,
  },
  { additionalProperties: false },
);

const causalRelation = Type.Object(
  {
    from_incident_id: nonEmptyString,
    to_incident_id: nonEmptyString,
    relationship: nonEmptyString,
    evidence: evidenceList,
  },
  { additionalProperties: false },
);

const trajectoryErrorReport = Type.Object(
  {
    schema: StringEnum(["analysis.trajectory_error_report.v1"] as const),
    role: StringEnum(["trajectory_error_analyst"] as const),
    run_id: nonEmptyString,
    precheck: Type.Object(
      {
        report_path: nonEmptyString,
        deterministic_status: nonEmptyString,
        integrity_status: StringEnum(
          ["valid", "invalid", "incomplete"] as const,
        ),
        interpreted_signal_ids: stringList,
        uninterpreted_signal_ids: stringList,
      },
      { additionalProperties: false },
    ),
    trajectory_assessment: StringEnum(
      [
        "no_observed_error",
        "errors_recovered",
        "terminal_failure",
        "incomplete_or_indeterminate",
        "invalid_or_inconsistent",
        "insufficient_evidence",
      ] as const,
    ),
    primary_incident_id: nullableString,
    summary: nonEmptyString,
    summary_evidence: evidenceList,
    incidents: Type.Array(incident),
    causal_chain: Type.Array(causalRelation),
    skill_fix_applicability: StringEnum(
      ["yes", "no", "uncertain"] as const,
    ),
    repair_target: nullableString,
    additional_evidence_needed: stringList,
    limitations: stringList,
  },
  { additionalProperties: false },
);

export default function trajectoryErrorOutput(pi: ExtensionAPI): void {
  let submitted = false;

  pi.registerTool({
    name: "submit_trajectory_error_analysis",
    label: "提交单 trajectory 分析",
    description:
      "Submit the final one-trajectory semantic error report through a strict schema.",
    promptSnippet:
      "Submit the final one-trajectory analysis as validated structured data",
    promptGuidelines: [
      "Use submit_trajectory_error_analysis exactly once as the final action " +
        "for the one-trajectory error report.",
      "Do not return the report as ordinary text or inside a Markdown code fence.",
      "After submit_trajectory_error_analysis succeeds, do not emit another assistant response.",
    ],
    parameters: trajectoryErrorReport,
    async execute() {
      if (submitted) {
        throw new Error("A trajectory error report has already been submitted");
      }
      submitted = true;
      return {
        content: [
          {
            type: "text",
            text: "Structured trajectory error report accepted for final validation.",
          },
        ],
        details: {
          schema: "analysis.structured_submission.v1",
          accepted: true,
        },
        terminate: true,
      };
    },
  });
}
