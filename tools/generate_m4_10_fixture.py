"""Generate the deterministic synthetic M4-10 contract fixture."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_control_plane.artifact_store import LocalArtifactStore
from context_control_plane.external_skill_sources import (
    create_external_skill_source_snapshot,
    external_skill_tree_url,
)

GENERATED_AT = "2026-08-12T00:00:02Z"
RETRIEVED_AT = "2026-08-12T00:00:01Z"
OUTPUT = (
    ROOT
    / "experiments"
    / "skills"
    / "m4-10-external-skill-source-snapshot-v1alpha1.json"
)


def _requests() -> list[dict[str, str]]:
    specs = (
        (
            "openai.node-link-and-diagram-layout",
            "adapter.openai-plugins-git",
            "skill",
            "1" * 40,
            "plugins/build-web-data-visualization/skills/node-link-and-diagram-layout/SKILL.md",
            "https://raw.githubusercontent.com/openai/plugins",
        ),
        (
            "agent-skills.specification",
            "adapter.agent-skills-standard-git",
            "standard",
            "2" * 40,
            "docs/specification.mdx",
            "https://raw.githubusercontent.com/agentskills/agentskills",
        ),
        (
            "github.agent-supply-chain",
            "adapter.github-awesome-copilot-git",
            "skill",
            "3" * 40,
            "skills/agent-supply-chain/SKILL.md",
            "https://raw.githubusercontent.com/github/awesome-copilot",
        ),
    )
    requests = [
        {
            "source_id": source_id,
            "adapter_id": adapter_id,
            "adapter_version": "1.0.0",
            "resource_kind": resource_kind,
            "source_revision": revision,
            "source_path": source_path,
            "retrieval_url": f"{root}/{revision}/{source_path}",
        }
        for source_id, adapter_id, resource_kind, revision, source_path, root in specs
    ]
    requests.append(
        {
            "source_id": "skills-sh.index",
            "adapter_id": "adapter.skills-sh-index",
            "adapter_version": "1.0.0",
            "resource_kind": "catalog-index",
            "source_revision": "dynamic-index",
            "source_path": "index.json",
            "retrieval_url": "https://skills.sh/api/search?q=skills&limit=20",
        }
    )
    return requests


def _observations(requests: list[dict[str, str]]) -> dict[str, dict[str, object]]:
    observations = {}
    for request in requests:
        source_id = request["source_id"]
        marketplace = source_id == "skills-sh.index"
        license_ref = (
            None
            if marketplace
            else "CC-BY-4.0"
            if source_id == "agent-skills.specification"
            else "MIT"
        )
        observations[source_id] = {
            "final_url": request["retrieval_url"],
            "retrieved_at": RETRIEVED_AT,
            "acquisition": "https-get",
            "files": [
                {
                    "path": request["source_path"],
                    "kind": "file",
                    "content": f"# Contract fixture: {source_id}\n",
                }
            ],
            "expected_paths": [request["source_path"]],
            "license_ref": license_ref,
            "license_evidence": (
                None if marketplace else f"synthetic license evidence for {source_id}\n"
            ),
            "publisher_evidence": (
                None
                if marketplace
                else f"synthetic publisher evidence for {source_id}\n"
            ),
            "declared_capabilities": (
                ["filesystem:read"] if request["resource_kind"] == "skill" else []
            ),
        }
    return observations


class _FixtureRetriever:
    def __init__(self, observations: dict[str, dict[str, object]]):
        self.observations = observations

    def __call__(self, request: dict[str, str], *, max_bytes: int) -> dict[str, object]:
        del max_bytes
        observation = copy.deepcopy(self.observations[request["source_id"]])
        for item in observation["files"]:
            item["content"] = item["content"].encode("utf-8")
        for field in ("license_evidence", "publisher_evidence"):
            if observation[field] is not None:
                observation[field] = observation[field].encode("utf-8")
        return observation


class _FixtureTreeRetriever:
    def __init__(self, requests: list[dict[str, str]]):
        self.requests = {request["source_id"]: request for request in requests}

    def __call__(self, request: dict[str, str], *, max_bytes: int) -> dict[str, object]:
        del max_bytes
        source = self.requests[request["source_id"]]
        return {
            "final_url": external_skill_tree_url(source),
            "source_revision": source["source_revision"],
            "truncated": False,
            "entries": [{"path": source["source_path"], "kind": "file"}],
        }


def main() -> None:
    requests = _requests()
    observations = _observations(requests)
    with tempfile.TemporaryDirectory() as temporary:
        store = LocalArtifactStore(Path(temporary) / "artifacts")
        store.initialize()
        snapshot = create_external_skill_source_snapshot(
            requests,
            retriever=_FixtureRetriever(observations),
            tree_retriever=_FixtureTreeRetriever(requests),
            artifact_store=store,
            generator_version="1.0.0",
            generated_at=GENERATED_AT,
        )
    fixture = {
        "fixture_kind": "synthetic-contract",
        "requests": requests,
        "observations": observations,
        "snapshot": snapshot,
    }
    OUTPUT.write_text(
        json.dumps(fixture, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
