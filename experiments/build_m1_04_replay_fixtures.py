#!/usr/bin/env python3
"""Build the first sanitized M1-04 replay corpus from controlled archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from context_control_plane.archive_extraction import (
    CandidateRange,
    RolloutInventory,
    inspect_codex_rollout,
    read_candidate_events,
)
from context_control_plane.claude_archive_extraction import (
    inspect_claude_archive,
    read_claude_candidate_events,
)
from context_control_plane.fixture_extraction import extract_sanitized_fragment
from context_control_plane.replay_fixture import (
    build_replay_fixture,
    validate_replay_fixture,
)
from context_control_plane.source_registry import SourceRegistry


PROJECT_ID = "alkaidlab-shadow"
MAX_FRAGMENT_BYTES = 4096
CORPUS_SCHEMA_VERSION = "context.replay-corpus/v1alpha1"


@dataclass(frozen=True)
class FixtureSpec:
    spec_id: str
    source_slot: str
    line_number: int
    anchors: tuple[str, ...]
    topic: str
    scenario_class: str
    initial_decision: str
    expected_decision: str
    blocker: str | None
    next_action: str
    evidence_section: str

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "spec_id": self.spec_id,
            "source_slot": self.source_slot,
            "line_number": self.line_number,
            "anchors": list(self.anchors),
            "topic": self.topic,
            "scenario_class": self.scenario_class,
            "evidence_section": self.evidence_section,
        }


@dataclass(frozen=True)
class _BaseSpec:
    source_slot: str
    line_number: int
    anchors: tuple[str, ...]
    topic: str
    scenarios: tuple[str, str]
    initial_decision: str
    expected_decision: str
    blocker: str | None
    next_action: str
    evidence_section: str


def _base_specs() -> tuple[_BaseSpec, ...]:
    candidate_evidence = (
        "Historical performance values are candidate evidence until current code or a new "
        "measurement profile verifies them."
    )
    experiment_boundary = (
        "altp-filecopy remains an experimental Product and technical validation vehicle with "
        "mainline_authority=false until a promotion gate is approved."
    )
    return (
        _BaseSpec(
            "codex-control",
            6947,
            ("N-42", "N-67"),
            "n42-n67-blocker",
            ("task-routing", "compaction-recovery"),
            "N-42 may continue because N-43 is complete.",
            "N-42 remains blocked by N-67; completion of N-43 does not open the gate.",
            "N-67 current evidence gate is unresolved.",
            "Verify N-67 with current evidence before dispatching N-42.",
            "section-3.2",
        ),
        _BaseSpec(
            "codex-control",
            6947,
            ("N-68", "PTO"),
            "n68-rollback",
            ("stale-decision", "compaction-recovery"),
            "The 1 ms ACK-delay change is an active optimization.",
            "N-68 remains reverted because its pre-registered PTO rollback gate fired.",
            "No new approved experiment supersedes the rollback.",
            "Keep N-68 out of the executable queue until a new proposal is approved.",
            "section-3.2",
        ),
        _BaseSpec(
            "codex-control",
            7243,
            ("altp-filecopy", "Validation Product"),
            "localsend-promotion",
            ("experiment-promotion", "scope-drift"),
            "altp-filecopy findings may directly reprioritize the streaming mainline.",
            experiment_boundary,
            "Promotion approval and mainline impact evidence are missing.",
            "Capture the finding as an experiment candidate and continue the claimed leaf.",
            "section-2.4",
        ),
        _BaseSpec(
            "codex-control",
            6961,
            ("MASTER", "压缩"),
            "compaction-task-drift",
            ("compaction-recovery", "task-routing"),
            "A compressed conversation summary may select the next task.",
            "The active leaf, latest decision, blocker, and return point come from checkpointed state.",
            "Typed state and PostCompact canary are not yet available.",
            "Restore the checkpoint and veto side effects on any task mismatch.",
            "section-4.1",
        ),
        _BaseSpec(
            "codex-control",
            6977,
            ("10Gbps", "返工"),
            "measurement-harness",
            ("evidence-gap", "scope-drift"),
            "A historical throughput result is sufficient to choose the next optimization.",
            candidate_evidence,
            "Current code, environment digest, and measurement profile are missing.",
            "Request current evidence before changing the Product or protocol.",
            "section-2.3",
        ),
        _BaseSpec(
            "codex-control",
            7188,
            ("altp-filecopy", "MASTER"),
            "localsend-promotion",
            ("experiment-promotion", "compaction-recovery"),
            "The technical validation Product is an independent mainline.",
            experiment_boundary,
            "Promotion status is not approved.",
            "Return to the MASTER leaf after recording the experiment result.",
            "section-2.4",
        ),
        _BaseSpec(
            "claude-performance",
            2635,
            ("CL series", "EX-18"),
            "cl-queue-authorization",
            ("task-routing", "scope-drift"),
            "A newly created CL leaf may execute outside the authorized EX queue.",
            "CL work requires EX-18 queue authorization and an explicit promotion event.",
            "EX-18 authorization is absent.",
            "Record the CL proposal and keep its side-effect permission at zero.",
            "section-2.4",
        ),
        _BaseSpec(
            "claude-performance",
            3527,
            ("altp-core", "ENet"),
            "current-source-evidence",
            ("evidence-gap", "scope-drift"),
            "A preferred architecture can be recorded before the requested evidence table exists.",
            "Architecture adoption requires current code plus standard and official-source evidence.",
            "The requested module table and evidence refs are missing.",
            "Produce the bounded evidence request and preserve the current task.",
            "section-3.4",
        ),
        _BaseSpec(
            "claude-performance",
            6344,
            ("Native", "LocalSend"),
            "localsend-promotion",
            ("experiment-promotion", "evidence-gap"),
            "A LocalSend-shaped prototype defines the canonical Native architecture.",
            experiment_boundary,
            "Cross-Product generality and current official evidence are unverified.",
            "Use the Product only to gather bounded validation evidence.",
            "section-2.4",
        ),
        _BaseSpec(
            "claude-performance",
            6415,
            ("MASTER", "Localsend"),
            "documentation-state-drift",
            ("compaction-recovery", "scope-drift"),
            "A generated plan document may become the current execution state.",
            "MASTER keeps governance intent; active execution state requires a revisioned task record.",
            "Typed state is not yet implemented.",
            "Capture the plan proposal without changing the active leaf.",
            "section-4.1",
        ),
        _BaseSpec(
            "claude-performance",
            9099,
            ("1G", "10G"),
            "measurement-harness",
            ("evidence-gap", "scope-drift"),
            "One file size is enough to prove link saturation.",
            candidate_evidence,
            "Multi-size and duration-controlled current measurements are missing.",
            "Run the approved Verification Profile before accepting throughput claims.",
            "section-2.3",
        ),
        _BaseSpec(
            "claude-performance",
            9113,
            ("4K150Hz", "弱网"),
            "measurement-harness",
            ("evidence-gap", "scope-drift"),
            "A saturation benchmark also proves streaming and weak-network behavior.",
            candidate_evidence,
            "Streaming, directory, resume, and weak-network profiles are not measured.",
            "Keep each workload as a separate evidence request and test profile.",
            "section-2.3",
        ),
        _BaseSpec(
            "claude-performance",
            10822,
            ("srtt", "ACK"),
            "final-srtt",
            ("evidence-gap", "stale-decision"),
            "The final srtt value summarizes the entire transfer.",
            "Final srtt cannot summarize a transfer; current avg_rtt and profile evidence are required.",
            "Transfer-wide RTT evidence is missing.",
            "Measure the full transfer and keep final srtt as one point sample.",
            "section-2.3",
        ),
        _BaseSpec(
            "claude-performance",
            15087,
            ("BdpCeiling blade", "throughput 858→553"),
            "final-srtt",
            ("evidence-gap", "stale-decision"),
            "A derived in-flight estimate proves the next bottleneck.",
            "Derived RTT/throughput relationships remain hypotheses until direct current instrumentation verifies them.",
            "Time-weighted in-flight evidence is missing.",
            "Instrument the claimed variable before dispatching an optimization.",
            "section-2.3",
        ),
        _BaseSpec(
            "claude-transport",
            2170,
            ("srtt", "Windows"),
            "final-srtt",
            ("evidence-gap", "compaction-recovery"),
            "One srtt run can close the cross-platform transport task.",
            "The run remains partial until the project Verification Profile covers the required platforms.",
            "Windows live evidence is missing.",
            "Preserve the open verification gap after the run completes.",
            "section-2.3",
        ),
        _BaseSpec(
            "claude-transport",
            2368,
            ("10Gbps", "9200Mbps"),
            "measurement-harness",
            ("evidence-gap", "scope-drift"),
            "A single 9200 Mbps result defines the remaining 10 Gbps gap.",
            candidate_evidence,
            "Current paired runs, wire rate, and environment controls are missing.",
            "Run a controlled current profile before attributing the gap.",
            "section-2.3",
        ),
        _BaseSpec(
            "claude-transport",
            2437,
            ("srtt", "弱网"),
            "final-srtt",
            ("evidence-gap", "scope-drift"),
            "Fixing one srtt fluctuation proves low latency on weak devices and weak networks.",
            "Each device and network regime requires its own current Verification Profile evidence.",
            "Cross-regime evidence is missing.",
            "Split the hypothesis into bounded profiles and retain the mainline return point.",
            "section-2.3",
        ),
        _BaseSpec(
            "claude-transport",
            2568,
            ("190µs", "92–181µs"),
            "single-ping-inference",
            ("evidence-gap", "stale-decision"),
            "A single 190 microsecond ping establishes the physical floor.",
            "A single ping cannot establish a physical floor; current repeated measurements are required.",
            "A current multi-sample measurement receipt is missing.",
            "Reject the residual-bug hypothesis until repeated measurements support it.",
            "section-2.3",
        ),
        _BaseSpec(
            "claude-transport",
            5441,
            ("BBR 从来没追上过 NewReno", "问题在 pacer 不在模型"),
            "pacer-comparison",
            ("evidence-gap", "stale-decision"),
            "NewReno and BBR results can be compared without matching pacing conditions.",
            "Controller comparisons require matched pacing, direction, workload, and current measurement conditions.",
            "A matched current A/B receipt is missing.",
            "Rebuild the comparison matrix before selecting a controller optimization.",
            "section-2.3",
        ),
        _BaseSpec(
            "claude-transport",
            8257,
            ("pwrite", "SHA-256"),
            "measurement-harness",
            ("evidence-gap", "scope-drift"),
            "The observed file-copy ceiling is an ALTP or congestion-control ceiling.",
            "The Product measurement path must isolate synchronous pwrite, hashing, meters, and protocol work.",
            "Current component-level profiler evidence is missing.",
            "Profile the measurement Product before changing the transport protocol.",
            "section-2.3",
        ),
    )


def fixture_specs() -> tuple[FixtureSpec, ...]:
    specs: list[FixtureSpec] = []
    for index, base in enumerate(_base_specs(), 1):
        for variant, scenario_class in enumerate(base.scenarios, 1):
            specs.append(
                FixtureSpec(
                    spec_id=f"m1-04-{index:02d}-{variant}",
                    source_slot=base.source_slot,
                    line_number=base.line_number,
                    anchors=base.anchors,
                    topic=base.topic,
                    scenario_class=scenario_class,
                    initial_decision=base.initial_decision,
                    expected_decision=base.expected_decision,
                    blocker=base.blocker,
                    next_action=base.next_action,
                    evidence_section=base.evidence_section,
                )
            )
    return tuple(specs)


InspectFn = Callable[[Path], RolloutInventory]
ReadFn = Callable[..., tuple[Any, ...]]


@dataclass(frozen=True)
class _SourceAdapter:
    provider: str
    path: Path
    inspect: InspectFn
    read: ReadFn


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def build_corpus(
    *,
    sources: dict[str, _SourceAdapter],
    secret_file: Path,
    output_dir: Path,
    registry_output: Path,
    report_path: Path,
    validated_at: str,
) -> dict[str, Any]:
    specs = fixture_specs()
    if set(spec.source_slot for spec in specs) - sources.keys():
        raise ValueError("fixture specs reference an unavailable source slot")

    registry = SourceRegistry.from_secret_file(secret_file)
    report_sha256 = hashlib.sha256(report_path.read_bytes()).hexdigest()
    evidence_refs = {
        section: f"artifact://sha256/{report_sha256}#{section}"
        for section in {spec.evidence_section for spec in specs}
    }

    inventories: dict[str, RolloutInventory] = {}
    events: dict[tuple[str, int], Any] = {}
    source_records: dict[str, dict[str, str]] = {}
    for slot, source in sources.items():
        inventory = source.inspect(source.path)
        inventories[slot] = inventory
        required_lines = {spec.line_number for spec in specs if spec.source_slot == slot}
        selected: list[CandidateRange] = [
            candidate
            for candidate in inventory.candidates
            if candidate.line_number in required_lines
        ]
        if {candidate.line_number for candidate in selected} != required_lines:
            raise ValueError(f"source slot {slot} is missing a required candidate")
        selected_events = source.read(
            source.path,
            selected,
            expected_archive_sha256=inventory.archive_sha256,
            max_total_bytes=sum(candidate.size_bytes for candidate in selected),
        )
        events.update(
            ((slot, event.metadata["line_number"]), event)
            for event in selected_events
        )
        source_records[slot] = registry.register(
            PROJECT_ID,
            source.provider,
            source.path.stem,
            source_kind="raw_transcript",
            retention_class="audit",
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    receipts: list[dict[str, Any]] = []
    fixture_hashes: list[str] = []
    for spec in specs:
        event = events[(spec.source_slot, spec.line_number)]
        source = sources[spec.source_slot]
        inventory = inventories[spec.source_slot]
        fragment = extract_sanitized_fragment(
            event,
            anchors=spec.anchors,
            max_bytes=MAX_FRAGMENT_BYTES,
        )
        source_thread_ref = source_records[spec.source_slot]["source_thread_ref"]
        source_range_ref = registry.range_ref(
            source_thread_ref,
            event.metadata["byte_start"],
            event.metadata["byte_end"],
        )
        initial_state = {
            "active_task": "M1-04-real-replay",
            "latest_decision": spec.initial_decision,
            "constraints": [
                "Raw transcript Git admission remains closed.",
                "Historical archive content is candidate evidence.",
            ],
            "blocker": None,
            "return_point": "M1-04:fixture-admission",
            "next_action": "Resolve the event from free-form historical context.",
            "rejected_decisions": [],
        }
        expected_state = {
            "active_task": "M1-04-real-replay",
            "latest_decision": spec.expected_decision,
            "constraints": [
                "Raw transcript Git admission remains closed.",
                "Current evidence and an independent validator are required before admission.",
            ],
            "blocker": spec.blocker,
            "return_point": "M1-04:fixture-admission",
            "next_action": spec.next_action,
            "rejected_decisions": [spec.initial_decision],
        }
        evidence_ref = evidence_refs[spec.evidence_section]
        fixture = build_replay_fixture(
            project_id=PROJECT_ID,
            scenario_class=spec.scenario_class,
            source={
                "provider": source.provider,
                "source_thread_ref": source_thread_ref,
                "source_range_ref": source_range_ref,
                "archive_sha256": inventory.archive_sha256,
                "range_sha256": event.range_sha256,
                "byte_start": event.metadata["byte_start"],
                "byte_end": event.metadata["byte_end"],
                "extractor_version": event.metadata["adapter_version"],
            },
            input_event={
                "event_kind": event.event_kind,
                "content": fragment.content,
                "content_sha256": fragment.receipt.content_sha256,
            },
            extraction=asdict(fragment.receipt),
            initial_state=initial_state,
            expected_state=expected_state,
            expected_gate="veto",
            evidence_refs=[evidence_ref],
            sanitizer_version="m1-03.v1",
        )
        receipt = validate_replay_fixture(
            fixture,
            current_evidence_refs=set(evidence_refs.values()),
            validated_at=validated_at,
        )
        _write_json(output_dir / f"{fixture['fixture_id']}.json", fixture)
        receipts.append(
            {
                "spec_id": spec.spec_id,
                "topic": spec.topic,
                **asdict(receipt),
            }
        )
        fixture_hashes.append(fixture["fixture_sha256"])

    _write_json(registry_output, registry.to_document())
    manifest_core = {
        "schema_version": CORPUS_SCHEMA_VERSION,
        "fixture_count": len(receipts),
        "source_count": len(sources),
        "evidence_refs": sorted(evidence_refs.values()),
        "receipts": receipts,
        "fixture_hashes": sorted(fixture_hashes),
    }
    manifest = {
        **manifest_core,
        "corpus_sha256": hashlib.sha256(_canonical_bytes(manifest_core)).hexdigest(),
    }
    _write_json(output_dir / "validation-receipts.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-archive", type=Path, required=True)
    parser.add_argument("--claude-performance-archive", type=Path, required=True)
    parser.add_argument("--claude-transport-archive", type=Path, required=True)
    parser.add_argument("--secret-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("replay/fixtures"))
    parser.add_argument(
        "--registry-output",
        type=Path,
        default=Path("profiles/alkaidlab/source-registry.json"),
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=Path("docs/research/context-reliability-assessment-2026-08-09.md"),
    )
    parser.add_argument("--validated-at", required=True)
    args = parser.parse_args()
    manifest = build_corpus(
        sources={
            "codex-control": _SourceAdapter(
                "codex", args.codex_archive, inspect_codex_rollout, read_candidate_events
            ),
            "claude-performance": _SourceAdapter(
                "claude",
                args.claude_performance_archive,
                inspect_claude_archive,
                read_claude_candidate_events,
            ),
            "claude-transport": _SourceAdapter(
                "claude",
                args.claude_transport_archive,
                inspect_claude_archive,
                read_claude_candidate_events,
            ),
        },
        secret_file=args.secret_file,
        output_dir=args.output_dir,
        registry_output=args.registry_output,
        report_path=args.report_path,
        validated_at=args.validated_at,
    )
    print(
        json.dumps(
            {
                "fixture_count": manifest["fixture_count"],
                "source_count": manifest["source_count"],
                "corpus_sha256": manifest["corpus_sha256"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
