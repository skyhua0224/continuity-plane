import base64
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml
from jsonschema import Draft202012Validator

from context_control_plane.git_admission import (
    GitAdmissionError,
    audit_first_commit,
    audit_regular_merge,
    audit_staged_admission,
    validate_git_collaboration_packet,
)


class GitAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(__file__).parents[1]

    def _packet(self) -> dict:
        return {
            "schema_version": "context.git-collaboration-packet/v1alpha1",
            "branch": {
                "branch_id": "work/M0-11/git-admission",
                "parent_id": "M0",
                "scope": "governance/git-admission",
                "return_point": "M0-10.acceptance-revision-4",
                "exit_criteria": ["git-admission-contract", "repository-verification"],
                "attempt_budget": 1,
                "expiry": None,
                "promotion_target": "main",
                "mainline_authority": False,
            },
            "commit": {
                "subject": "feat(governance): add Git admission contract",
                "body": (
                    "Why: Preserve a reviewable Git boundary without granting runtime "
                    "state authority.\n\n"
                    "Task: M0-11\n"
                    "State-Revision: 40\n"
                    "Evidence: docs/migrations/m0-11-git-admission-acceptance.md\n"
                    "Tests: python -m unittest tests.test_m0_11_git_admission"
                ),
            },
            "pull_request": {
                "title": "feat(governance): add Git admission contract",
                "body": (
                    "Why:\n"
                    "Git remains an audit boundary.\n\n"
                    "Scope:\n"
                    "Git packet and staged admission.\n\n"
                    "State and ownership:\n"
                    "work_id: M0-11\n"
                    "parent_id: M0\n"
                    "scope: governance/git-admission\n"
                    "return_point: M0-10.acceptance-revision-4\n"
                    "exit_criteria: [git-admission-contract, repository-verification]\n"
                    "attempt_budget: 1\n"
                    "expiry: null\n"
                    "promotion_target: main\n"
                    "mainline_authority: false\n"
                    "state_revision: 40\n"
                    "claim_ref: claim://local-shadow/m0-11\n"
                    "path_owner: actor://opaque/local-owner\n"
                    "evidence_refs: [artifact://sha256/"
                    + "a" * 64
                    + "]\n"
                    "verification_profile: repository-verification\n\n"
                    "Evidence:\n"
                    "artifact://sha256/"
                    + "a" * 64
                    + "\n\n"
                    "Validation:\n"
                    "python -m unittest tests.test_m0_11_git_admission\n\n"
                    "Risk and rollback:\n"
                    "Revert the Git boundary without changing runtime state.\n\n"
                    "Open questions:\n"
                    "None."
                ),
            },
        }

    def _date(self) -> str:
        return "-".join(("2026", "08", "12"))

    def _test_email(self) -> str:
        return "git-admission" + "@" + "example.invalid"

    def _git(self, root: Path, *arguments: str) -> str:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def _commit(self, root: Path, message: str, path: str = "artifact.txt") -> str:
        (root / path).write_text(message + "\n", encoding="utf-8")
        self._git(root, "add", path)
        self._git(root, "commit", "-m", message)
        return self._git(root, "rev-parse", "HEAD")

    def _repository(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self._git(root, "init", "-q", "-b", "main")
        self._git(root, "config", "user.name", "Git Admission Test")
        self._git(root, "config", "user.email", self._test_email())
        self._commit(
            root,
            "chore(repo): establish provider-neutral repository boundary\n\n"
            "Why: Preserve an auditable repository boundary.\n\n"
            "Task: M0-01\n"
            "State-Revision: 1\n"
            "Evidence: artifact://sha256/" + "a" * 64 + "\n"
            "Tests: python -m unittest",
        )
        return root

    def test_validates_a_provider_neutral_branch_commit_and_pr_packet(self):
        validate_git_collaboration_packet(self._packet())

    def test_rejects_raw_provider_identity_in_branch_packet(self):
        packet = self._packet()
        packet["branch"]["branch_id"] = (
            "work/M0-11/git-admission--"
            + "-".join(
                ("019fe216", "111c", "71e3", "a1af", "497306ba2391")
            )
        )

        with self.assertRaisesRegex(GitAdmissionError, "branch_id"):
            validate_git_collaboration_packet(packet)

    def test_rejects_commit_without_machine_parseable_evidence_trailers(self):
        packet = self._packet()
        packet["commit"]["body"] = "Why: missing delivery evidence"

        with self.assertRaisesRegex(GitAdmissionError, "commit body"):
            validate_git_collaboration_packet(packet)

    def test_rejects_commit_without_a_why_statement(self):
        packet = self._packet()
        packet["commit"]["body"] = (
            "Task: M0-11\n"
            "State-Revision: 40\n"
            "Evidence: docs/migrations/m0-11-git-admission-acceptance.md\n"
            "Tests: python -m unittest tests.test_m0_11_git_admission"
        )

        with self.assertRaisesRegex(GitAdmissionError, "Why"):
            validate_git_collaboration_packet(packet)

    def test_rejects_pr_that_claims_mainline_authority(self):
        packet = self._packet()
        packet["pull_request"]["body"] = packet["pull_request"]["body"].replace(
            "mainline_authority: false", "mainline_authority: true"
        )

        with self.assertRaisesRegex(GitAdmissionError, "mainline_authority"):
            validate_git_collaboration_packet(packet)

    def test_rejects_pr_packet_that_names_a_different_work_than_its_branch(self):
        packet = self._packet()
        packet["pull_request"]["body"] = packet["pull_request"]["body"].replace(
            "work_id: M0-11", "work_id: M0-12"
        )

        with self.assertRaisesRegex(GitAdmissionError, "work_id"):
            validate_git_collaboration_packet(packet)

    def test_rejects_pr_packet_with_a_different_commit_revision(self):
        packet = self._packet()
        packet["pull_request"]["body"] = packet["pull_request"]["body"].replace(
            "state_revision: 40", "state_revision: 41"
        )

        with self.assertRaisesRegex(GitAdmissionError, "state_revision"):
            validate_git_collaboration_packet(packet)

    def test_rejects_pr_packet_with_a_different_exit_criteria(self):
        packet = self._packet()
        packet["pull_request"]["body"] = packet["pull_request"]["body"].replace(
            "exit_criteria: [git-admission-contract, repository-verification]",
            "exit_criteria: [git-admission-contract]",
        )

        with self.assertRaisesRegex(GitAdmissionError, "exit_criteria"):
            validate_git_collaboration_packet(packet)

    def test_rejects_pr_packet_with_a_different_expiry_than_its_branch(self):
        packet = self._packet()
        packet["branch"]["expiry"] = "-".join(("2026", "12", "31")) + "T00:00:00Z"

        with self.assertRaisesRegex(GitAdmissionError, "expiry"):
            validate_git_collaboration_packet(packet)

    def test_rejects_pr_packet_with_a_different_title_than_its_commit(self):
        packet = self._packet()
        packet["pull_request"]["title"] = "fix(governance): change another contract"

        with self.assertRaisesRegex(GitAdmissionError, "title"):
            validate_git_collaboration_packet(packet)

    def test_staged_admission_rejects_raw_transcript_secret_and_private_path(self):
        cases = {
            "provider-session.jsonl": b'{"type":"message"}\n',
            "credentials.txt": base64.b64decode(
                b"QXV0aG9yaXphdGlvbjogQmVhcmVyIHByb2Qtc2VjcmV0LXZhbHVlLXdpdGgtZW50cm9weS0xMjM0NTY3ODkw"
            ),
            "notes.txt": base64.b64decode(
                b"d29ya3NwYWNlPS9ob21lL2FsaWNlL1Byb2plY3RzL3ByaXZhdGUtcmVwbw=="
            ),
        }
        for path, content in cases.items():
            with self.subTest(path=path), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self._git(root, "init", "-q")
                (root / path).write_bytes(content)
                self._git(root, "add", path)

                with self.assertRaises(GitAdmissionError):
                    audit_staged_admission(root)

    def test_staged_admission_rejects_an_empty_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git(root, "init", "-q")

            with self.assertRaisesRegex(GitAdmissionError, "staged paths"):
                audit_staged_admission(root)

    def test_staged_admission_rejects_an_unvalidated_replay_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git(root, "init", "-q")
            fixture = root / "replay" / "fixtures" / "unbound.json"
            fixture.parent.mkdir(parents=True)
            fixture.write_text('{"scenario_class":"task-routing"}\n', encoding="utf-8")
            self._git(root, "add", "replay/fixtures/unbound.json")

            with self.assertRaisesRegex(GitAdmissionError, "provenance"):
                audit_staged_admission(root)

    def test_staged_admission_accepts_a_sanitized_small_fixture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git(root, "init", "-q")
            (root / "replay" / "fixtures").mkdir(parents=True)
            (root / "replay" / "fixtures" / "switch.yaml").write_text(
                "scenario: task-switch\nsource_thread_ref: thr_abcdefghijklmnopqrstuvwxy\n",
                encoding="utf-8",
            )
            self._git(root, "add", "replay/fixtures/switch.yaml")

            receipt = audit_staged_admission(root)

        self.assertEqual(receipt["admitted_paths"], ["replay/fixtures/switch.yaml"])
        self.assertEqual(receipt["runtime_state_authority"], False)

    def test_staged_admission_accepts_sha256_integrity_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git(root, "init", "-q")
            digest = "abcdef" * 10 + "abcd"
            (root / "benchmark.yaml").write_text(
                "generated_at: '" + self._date() + "'\n"
                "current_status_sha256: " + digest + "\n",
                encoding="utf-8",
            )
            self._git(root, "add", "benchmark.yaml")

            receipt = audit_staged_admission(root)

        self.assertEqual(receipt["admitted_paths"], ["benchmark.yaml"])

    def test_staged_admission_accepts_protocol_integrity_references(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git(root, "init", "-q")
            digest = "abcdef" * 10 + "abcd"
            (root / "provenance.yaml").write_text(
                "generated_at: '" + self._date() + "'\n"
                "authority_ref: verification-run://repository/" + digest + "\n"
                "git_commit: " + "1" * 40 + "\n",
                encoding="utf-8",
            )
            self._git(root, "add", "provenance.yaml")

            receipt = audit_staged_admission(root)

        self.assertEqual(receipt["admitted_paths"], ["provenance.yaml"])

    def test_staged_admission_rejects_a_bearer_token_with_sha256_shape(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git(root, "init", "-q")
            bearer = "Authorization: Bearer " + "a" * 64 + "\n"
            (root / "credentials.txt").write_text(bearer, encoding="utf-8")
            self._git(root, "add", "credentials.txt")

            with self.assertRaises(GitAdmissionError):
                audit_staged_admission(root)

    def test_staged_admission_reads_the_index_not_unstaged_worktree_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git(root, "init", "-q")
            (root / "fixture.yaml").write_text("scenario: clean\n", encoding="utf-8")
            self._git(root, "add", "fixture.yaml")
            unstaged = base64.b64decode(
                b"QXV0aG9yaXphdGlvbjogQmVhcmVyIHNlY3JldC10aGF0LWlzLW5vdC1zdGFnZWQtMTIzNDU2Nzg5MAo="
            ).decode("utf-8")
            (root / "fixture.yaml").write_text(unstaged, encoding="utf-8")

            receipt = audit_staged_admission(root)

        self.assertEqual(receipt["admitted_paths"], ["fixture.yaml"])

    def test_packet_schema_is_registered_and_accepts_the_contract_packet(self):
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.git-collaboration-packet"
        )
        artifact = self.root / entry["artifact_path"]
        self.assertEqual(
            hashlib.sha256(artifact.read_bytes()).hexdigest(), entry["content_sha256"]
        )
        schema = json.loads(artifact.read_text(encoding="utf-8"))

        self.assertEqual(list(Draft202012Validator(schema).iter_errors(self._packet())), [])

    def test_receipt_schema_is_registered_and_accepts_all_audit_receipts(self):
        registry = yaml.safe_load(
            (self.root / "schemas" / "registry.yaml").read_text(encoding="utf-8")
        )
        entry = next(
            item
            for item in registry["schemas"]
            if item["schema_id"] == "context.git-admission-receipt"
        )
        artifact = self.root / entry["artifact_path"]
        self.assertEqual(
            hashlib.sha256(artifact.read_bytes()).hexdigest(), entry["content_sha256"]
        )
        schema = json.loads(artifact.read_text(encoding="utf-8"))
        root = self._repository()
        (root / "fixture.yaml").write_text("scenario: clean\n", encoding="utf-8")
        self._git(root, "add", "fixture.yaml")
        staged = audit_staged_admission(root)
        base = self._git(root, "rev-parse", "HEAD")
        self._git(root, "checkout", "-q", "-b", "work/M0-11/receipt-fixture")
        self._commit(root, "feat(governance): add receipt fixture", "source.txt")
        self._git(root, "checkout", "-q", "main")
        self._commit(root, "docs(governance): update receipt baseline")
        self._git(
            root,
            "merge",
            "--no-ff",
            "work/M0-11/receipt-fixture",
            "-m",
            "Merge pull request 'feat(governance): add receipt fixture'",
        )
        merge = audit_regular_merge(root, self._git(root, "rev-parse", "HEAD"))
        first_commit = audit_first_commit(root)

        self.assertEqual(base, first_commit["root_commit"])
        for receipt in (staged, merge, first_commit):
            with self.subTest(admission_kind=receipt["admission_kind"]):
                self.assertEqual(
                    list(Draft202012Validator(schema).iter_errors(receipt)), []
                )

    def test_audits_regular_merge_with_both_parent_commits_preserved(self):
        root = self._repository()
        base = self._git(root, "rev-parse", "HEAD")
        self._git(root, "checkout", "-q", "-b", "work/M0-11/merge-fixture")
        source = self._commit(
            root,
            "feat(governance): add merge fixture",
            path="source-artifact.txt",
        )
        self._git(root, "checkout", "-q", "main")
        self._commit(root, "docs(governance): update merge baseline")
        self._git(root, "merge", "--no-ff", "work/M0-11/merge-fixture", "-m", "Merge pull request 'feat(governance): add merge fixture'")
        merge = self._git(root, "rev-parse", "HEAD")

        receipt = audit_regular_merge(root, merge)

        self.assertEqual(receipt["merge_commit"], merge)
        self.assertEqual(receipt["source_parent"], source)
        self.assertNotEqual(receipt["target_parent"], base)
        self.assertEqual(receipt["runtime_state_authority"], False)

    def test_regular_merge_audit_rejects_a_single_parent_commit(self):
        root = self._repository()

        with self.assertRaisesRegex(GitAdmissionError, "regular merge"):
            audit_regular_merge(root, self._git(root, "rev-parse", "HEAD"))

    def test_first_commit_audit_replays_author_and_admission_contracts(self):
        root = self._repository()

        receipt = audit_first_commit(root)

        self.assertEqual(receipt["commit_count_before_root"], 0)
        self.assertEqual(receipt["runtime_state_authority"], False)
        self.assertEqual(receipt["root_commit"], self._git(root, "rev-list", "--max-parents=0", "HEAD"))

    def test_first_commit_audit_rejects_missing_commit_trailers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git(root, "init", "-q", "-b", "main")
            self._git(root, "config", "user.name", "Git Admission Test")
            self._git(root, "config", "user.email", self._test_email())
            self._commit(root, "chore(repo): establish provider-neutral repository boundary")

            with self.assertRaisesRegex(GitAdmissionError, "commit body"):
                audit_first_commit(root)

    def test_first_commit_audit_rejects_a_raw_transcript_in_the_root_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._git(root, "init", "-q", "-b", "main")
            self._git(root, "config", "user.name", "Git Admission Test")
            self._git(root, "config", "user.email", self._test_email())
            self._commit(
                root,
                "chore(repo): establish provider-neutral repository boundary\n\n"
                "Why: Preserve an auditable repository boundary.\n\n"
                "Task: M0-01\n"
                "State-Revision: 1\n"
                "Evidence: artifact://sha256/" + "a" * 64 + "\n"
                "Tests: python -m unittest",
                path="provider-session.jsonl",
            )

            with self.assertRaisesRegex(GitAdmissionError, "raw transcript"):
                audit_first_commit(root)


if __name__ == "__main__":
    unittest.main()
