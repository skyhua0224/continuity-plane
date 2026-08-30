# M11-00 Tiered Plugin Skill Move

版本：1  
日期：2026-08-30  
状态：verified migration record

## Source

| Field | Value |
|---|---|
| document | `context.document://integrations-codex-continuity-plane-skills-continuity-plane-skill/revision/5` |
| path | `integrations/codex/continuity-plane/skills/continuity-plane/SKILL.md` |
| content SHA-256 | `7a08fa92bfde982e7c1d808b7a01317066381c6eea819b24ed64156b35ab0363` |
| disposition | moved to advanced State plugin |

## Destination

The State workflow contract is located at
`integrations/codex/continuity-plane-state/skills/continuity-plane/SKILL.md`.
The default core plugin contains lifecycle hooks and a bounded startup projection.
Large-repository search guidance is isolated in the optional
`continuity-plane-search` plugin.

## Verification

Plugin profile and public release projection tests verify that core, search, and
State capabilities remain separately installable. The migration grants no active
State, completion, memory, or external-effect authority.
