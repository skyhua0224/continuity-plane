"""Branch, HEAD and merge-base of a worktree, read from git instead of from a worker's report."""

import hashlib
import re
from pathlib import Path

_MAX_REPORT_BYTES = 65536
_BASE_CLAIM = re.compile(
    r"\bbase(?:\s+commit)?\b\s*[:=@]?\s*[`'\"]?((?=[0-9a-f]*\d)[0-9a-f]{7,40})\b",
    re.IGNORECASE,
)


def worktree_facts(workdir, integration_ref=None, integration_head=None):
    """Facts about the checkout at `workdir`, or None when it is not a git worktree with a HEAD."""
    from .lifecycle import _git

    try:
        path = Path(workdir)
        if not path.is_dir():
            return None
    except (OSError, TypeError, ValueError):
        return None
    top = _git(path, "rev-parse", "--show-toplevel")
    head = _git(path, "rev-parse", "--verify", "HEAD^{commit}") if top else None
    if not head:
        return None
    merge_base, used = None, None
    for target in (integration_head, integration_ref):
        if target:
            merge_base = _git(path, "merge-base", "HEAD", target)
            if merge_base:
                used = target
                break
    return {
        "workdir": top,
        "branch": _git(path, "symbolic-ref", "--quiet", "--short", "HEAD"),
        "head": head,
        "merge_base": merge_base,
        "integration": integration_ref or integration_head,
        "merge_base_against": used,
        "dirty": bool(_git(path, "status", "--porcelain", "--untracked-files=no")),
    }


def claimed_bases(text):
    """Commit ids a report states as its base ("base 1a2b3c4", "Base commit: `…`")."""
    found = []
    for match in _BASE_CLAIM.finditer(text or ""):
        if match.group(1) not in found:
            found.append(match.group(1))
    return found


def _same(claimed, actual):
    claimed = (claimed or "").strip().lower()
    return bool(actual) and len(claimed) >= 7 and actual.lower().startswith(claimed)


def mismatches(facts, *, branch=None, commit=None, base=None, text=None):
    """Values a worker supplied (or wrote in prose) that the worktree contradicts."""
    found = []
    if branch and facts["branch"] != branch:
        found.append(
            f"branch: reported {branch}, the worktree is on {facts['branch'] or 'a detached HEAD'}"
        )
    if commit and not _same(commit, facts["head"]):
        found.append(f"commit: reported {commit}, the worktree HEAD is {facts['head']}")
    if facts["merge_base"]:
        claims = [base] if base else []
        claims += [c for c in claimed_bases(text) if c != base]
        for claim in claims:
            if not _same(claim, facts["merge_base"]):
                found.append(
                    f"base: reported {claim}, the merge-base with "
                    f"{facts['merge_base_against']} is {facts['merge_base']}"
                )
    return found


def section(facts, problems, workdir=None):
    """Markdown lines stating what git says, with each mismatch called out."""
    lines = ["## Repository facts (from git, not from the worker)", ""]
    if not facts:
        return lines + [
            f"- Not verified: no git worktree at `{workdir}`; branch, HEAD and base are unchecked.",
            "",
        ]
    lines += [
        f"- Worktree: `{facts['workdir']}`",
        f"- Branch: `{facts['branch'] or 'detached HEAD'}`",
        f"- HEAD: `{facts['head']}`",
    ]
    if facts["merge_base"]:
        lines.append(
            f"- Merge-base with `{facts['merge_base_against']}`: `{facts['merge_base']}`"
        )
    else:
        lines.append(
            "- Merge-base: not determined (integration ref "
            f"`{facts['integration'] or 'unknown'}` is not reachable from this worktree)"
        )
    if facts["dirty"]:
        lines.append("- Uncommitted changes: yes; HEAD does not contain all of the work")
    lines += [f"- MISMATCH: {p}" for p in problems]
    return lines + [""]


def inspect(workdir, *, data_root, project_id, branch=None, commit=None, base=None, text=None):
    """(facts or None, mismatch list, report section lines) for the checkout at `workdir`."""
    ref = head = None
    try:
        from .project_context import integration

        selected = integration(data_root, project_id, workdir)
        ref, head = selected.get("ref"), selected.get("head")
    except Exception:
        pass
    facts = worktree_facts(workdir, ref, head)
    problems = (
        mismatches(facts, branch=branch, commit=commit, base=base, text=text)
        if facts
        else []
    )
    return facts, problems, section(facts, problems, workdir)


def annotate_report(report, workdir, *, data_root, project_id):
    """Append the git facts to an attached report; leave it untouched when git has nothing to say."""
    try:
        if not workdir:
            return report
        facts, _, lines = inspect(
            workdir, data_root=data_root, project_id=project_id, text=report["text"]
        )
        if not facts:
            return report
        text = report["text"].rstrip("\n") + "\n\n" + "\n".join(lines)
        content = text.encode()
        if len(content) > _MAX_REPORT_BYTES:
            return report
        return {
            **report,
            "text": text,
            "bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
            "source_sha256": report["sha256"],
        }
    except Exception:
        return report
