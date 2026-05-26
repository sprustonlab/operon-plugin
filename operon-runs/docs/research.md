# research.md -- Definitive claudechic inventory (full-removal directive)

**Author:** researcher
**Phase:** specification
**Date:** 2026-05-25
**Directive:** Get claudechic OUT of the repo ENTIRELY -- every occurrence,
including live tokens/identifiers, replaced by the operon equivalent. This
LIFTS the earlier "comment-only" limit.

## Method

```
grep -rni "claudechic"  (case-insensitive, whole repo)
  excluding: .git .venv __pycache__ .ruff_cache .pytest_cache
             .test_results node_modules
  also excluding uv.lock (verified 0 hits)
```

Plus explicit catches for the token `${CLAUDECHIC_ARTIFACT_DIR}`, the bare
identifier form `CLAUDECHIC_ARTIFACT_DIR`, and any other CLAUDECHIC-containing
identifier/string (none found beyond the token).

The external inspiration repo at `/groups/spruston/home/moharb/claudechic`
was NOT scanned (out of scope per directive).

## Headline numbers

- **Total "claudechic" hits (in scope): 90**, across **15 files**.
- Of those 90, **55 are in the four internal audit docs** under `docs/`
  (SPEC_GROUNDING_AUDIT.md 35, APPENDIX.MD 10, AGENT_TEAMS_PIVOT_PLAN.md 8,
  SCOPE_AUDIT_v2.1.md 0) -- flagged OPEN, see "Internal audit docs" below.
- **35 hits are in source/manifest/README/CHANGELOG** -- the actionable set.
- `${CLAUDECHIC_ARTIFACT_DIR}` token: **5 occurrences, all in 2 files**
  (workflow.py x4, project_team.yaml x1). **Zero live template usages,
  zero test usages.** See "Token mechanics" -- removal is behavior-safe.
- `CLAUDECHIC` appears in NO other identifier/string (only the token).
- `uv.lock`: 0 hits.

### Per-file hit counts

| File | claudechic hits |
|------|-----------------|
| docs/SPEC_GROUNDING_AUDIT.md | 35 (OPEN -- audit doc) |
| docs/APPENDIX.MD | 10 (OPEN -- audit doc) |
| docs/AGENT_TEAMS_PIVOT_PLAN.md | 8 (OPEN -- audit doc) |
| src/operon_mcp_server/workflow.py | 7 |
| plugins/operon-plugin/workflows/project_team/project_team.yaml | 7 |
| src/operon_mcp_server/rules.py | 6 |
| README.md | 4 |
| plugins/operon-plugin/rules.yaml | 4 |
| src/operon_mcp_server/checks/builtins.py | 3 |
| src/operon_mcp_server/tools/restore_operon_session.py | 1 |
| src/operon_mcp_server/tools/request_override.py | 1 |
| src/operon_mcp_server/checks/protocol.py | 1 |
| src/operon_mcp_server/bootstrap.py | 1 |
| plugins/operon-plugin/workflows/project_team/README.md | 1 |
| CHANGELOG.md | 1 |
| **TOTAL** | **90** |

docs/SCOPE_AUDIT_v2.1.md: 0 hits (listed only because the lead named it).

---

## Token mechanics (behavior-preserving rename) -- RESOLVED

**Question:** Does `workflow.py` substitute BOTH `${ARTIFACT_DIR}` (primary)
and `${CLAUDECHIC_ARTIFACT_DIR}` (alias)? **YES -- chained `.replace()`.**

Exact code, `src/operon_mcp_server/workflow.py:438-441` (in
`_expand_artifact_dir`):

```python
if isinstance(value, str):
    return value.replace("${ARTIFACT_DIR}", artifact_dir).replace(
        "${CLAUDECHIC_ARTIFACT_DIR}", artifact_dir
    )
```

`${ARTIFACT_DIR}` is the operon-native primary token and MUST be preserved.
`${CLAUDECHIC_ARTIFACT_DIR}` is a legacy alias kept "for portability" of
ported claudechic workflows (per the docstring at `:433-434`).

**Critical finding for completeness:** the CLAUDECHIC-prefixed token is
referenced in ONLY these 5 places, and NONE of them is a live template/YAML
that feeds a value through `_expand_artifact_dir`:

| file:line | what it is |
|-----------|-----------|
| src/operon_mcp_server/workflow.py:424 | docstring (mentions both spellings) |
| src/operon_mcp_server/workflow.py:433 | docstring ("ported claudechic workflows use ...") |
| src/operon_mcp_server/workflow.py:440 | the live `.replace()` alias arg |
| src/operon_mcp_server/workflow.py:461 | docstring in `_inject_seam_params` (mentions both) |
| plugins/operon-plugin/workflows/project_team/project_team.yaml:7 | comment documenting the rename |

I grepped every workflow YAML, every phase `.md` file, `_smoke.yaml`, and all
tests for the literal `${CLAUDECHIC_ARTIFACT_DIR}` spelling: **no hits.**
Every actual template path uses `${ARTIFACT_DIR}`. (Confirmed: the
`${ARTIFACT_DIR}` token is referenced live in project_team.yaml x9,
coordinator/setup.md x3, _smoke.yaml x3, the five `*/testing-specification.md`
files, set_artifact_dir.py, get_phase.py, query_protocol.py, tests, etc. --
all the PRIMARY spelling, never the CLAUDECHIC alias.)

**Therefore the alias is dead code for any current workflow.** Removing it is
behavior-preserving:

1. In `workflow.py:438-441`, drop the second `.replace(...)` so only
   `${ARTIFACT_DIR}` is substituted. (Behavior-affecting in the strict sense
   -- it changes the substitution function -- but no current input exercises
   the removed branch, so observable behavior for shipped workflows is
   unchanged. **Needs a regression test:** assert `${ARTIFACT_DIR}` still
   expands and that a string containing the old alias is now passed through
   literally / no longer special-cased.)
2. Delete the alias mentions in the three docstrings (`:424, :433, :461`).
3. Delete the project_team.yaml:7 comment line documenting the rename.

No `*.md`, YAML, or test edit is needed to keep templates working, because
none use the alias. This is the one genuinely behavior-touching item in the
whole inventory; everything else is pure text.

---

## Actionable inventory (source / manifest / README / CHANGELOG)

Legend -- **Behavior?**: Y = changes runtime behavior, needs a test;
N = pure text (comment / prose / docstring), no test needed.
Category: comment/prose | live-token | code-string | filename/dir.

### src/operon_mcp_server/workflow.py (7)

| line | current text | category | proposed operon replacement | Behavior? |
|------|--------------|----------|-----------------------------|-----------|
| 159 | `(claudechic convention -- \`project_team/project_team.yaml\`) then` | comment/prose | `(the bundled \`project_team/project_team.yaml\` convention) then` | N |
| 171 | `Accepts the claudechic shape::` | comment/prose | `Accepts the manifest shape::` | N |
| 182 | `name (mirrors claudechic's behavior for hand-rolled workflows).` | comment/prose | `name (the behavior for hand-rolled workflows).` | N |
| 424 | `Substitute \`${ARTIFACT_DIR}\` / \`${CLAUDECHIC_ARTIFACT_DIR}\` in` | comment/prose (re: token) | `Substitute \`${ARTIFACT_DIR}\` in` | N (paired with token removal below) |
| 433 | `Phase 11: ported claudechic workflows use \`${CLAUDECHIC_ARTIFACT_DIR}\`` | comment/prose (re: token) | delete the two-sentence "Phase 11 ... portability." note | N |
| 440 | `"${CLAUDECHIC_ARTIFACT_DIR}", artifact_dir` (the `.replace()` alias arg, lines 439-441) | **live-token** | drop the chained `.replace("${CLAUDECHIC_ARTIFACT_DIR}", artifact_dir)` so only `${ARTIFACT_DIR}` is substituted | **Y -- needs test** |
| 461 | `Also substitutes \`${ARTIFACT_DIR}\` / \`${CLAUDECHIC_ARTIFACT_DIR}\`` | comment/prose (re: token) | `Also substitutes \`${ARTIFACT_DIR}\`` | N |

### plugins/operon-plugin/workflows/project_team/project_team.yaml (7)

| line | current text | category | proposed operon replacement | Behavior? |
|------|--------------|----------|-----------------------------|-----------|
| 1 | `# Project Team workflow manifest -- operon port of claudechic's` | comment/prose | `# Project Team workflow manifest.` (or finish sentence without "claudechic") | N |
| 3 | `# rules mirror claudechic verbatim with the following surgical` | comment/prose | `# rules, with the following surgical` (rework the header comment block 1-12 as a unit) | N |
| 7 | `#   - Template token \`${CLAUDECHIC_ARTIFACT_DIR}\` -> \`${ARTIFACT_DIR}\`` | comment/prose (re: token) | DELETE the line (rename complete; nothing to document) | N |
| 9 | `#   - Coordinator-spec-edit rule's \`.claudechic/runs\` path fragment ->` | comment/prose | reword without "claudechic" (the live rule already uses `.operon`/dual at :98) | N |
| 11 | `#   - The \`hints:\` block on the specification phase from claudechic is` | comment/prose | `#   - The upstream \`hints:\` block on the specification phase is` | N |
| 12 | `#     omitted -- claudechic's pipeline-hints system is explicitly out` | comment/prose | `#     omitted -- the pipeline-hints system is out of scope here.` | N |
| 47 | `# claudechic's manifest declares a \`hints:\` block here ("Synthesize` | comment/prose | `# The upstream manifest declares a \`hints:\` block here ("Synthesize` | N |

NOTE: project_team.yaml:98 regex `(?i)(?:\.project_team|\.operon)/...spec...`
intentionally matches BOTH layouts and contains no "claudechic" string -- not
in this inventory, do NOT touch (it is live rule logic).

### src/operon_mcp_server/rules.py (6)

| line | current text | category | proposed operon replacement | Behavior? |
|------|--------------|----------|-----------------------------|-----------|
| 5 | ``\`claudechic/guardrails/rules.py\` verbatim into a single file because`` | comment/prose | reword the module docstring to describe operon's own rules module without the claudechic source path | N |
| 12 | `claudechic's existing schema so a user can hand-edit` | comment/prose | `the existing schema so a user can hand-edit` | N |
| 18 | `Rule entries OR a \`{"rules": [...]}\` wrapper (claudechic uses the` | comment/prose | `Rule entries OR a \`{"rules": [...]}\` wrapper (the` | N |
| 86 | `"""Parsed manifest entry. Mirrors claudechic's \`Rule\` shape."""` | comment/prose | `"""Parsed manifest entry."""` | N |
| 353 | `# Bare "PreToolUse" matches all tools (claudechic convention).` | comment/prose | `# Bare "PreToolUse" matches all tools.` | N |
| 400 | `Pure function. The order mirrors claudechic's pipeline: trigger` | comment/prose | `Pure function. The pipeline order is: trigger` | N |

### plugins/operon-plugin/rules.yaml (4)

| line | current text | category | proposed operon replacement | Behavior? |
|------|--------------|----------|-----------------------------|-----------|
| 8 | `# Schema (per claudechic convention; mirrored in` | comment/prose | `# Schema (mirrored in` | N |
| 24 | `# 1) Catastrophic-rm hard block. Mirrors claudechic global rule` | comment/prose | `# 1) Catastrophic-rm hard block.` (drop Mirrors clause; check line 25 continuation for a trailing rule name to drop too) | N |
| 44 | `# 3) Sudo acknowledgment. Mirrors claudechic's \`warn_sudo\`. Any` | comment/prose | `# 3) Sudo acknowledgment. Any` | N |
| 54 | `#    block, no prompt). Mirrors claudechic's \`log_git_operations\`.` | comment/prose | `#    block, no prompt).` | N |

NOTE: rules.yaml comments NAME the rule ids (`warn_sudo`, `log_git_operations`).
Those ids are operon's OWN rule ids (confirmed live in the manifest via the
get_applicable_rules reply: warn_sudo, log_git_operations, no_rm_rf, etc.).
Only the "Mirrors claudechic's" framing is removed; the rule-id references
that happen to share claudechic's names are operon's real ids -- leave the
ids, drop the attribution.

### README.md (4)

| line | current text | category | proposed operon replacement | Behavior? |
|------|--------------|----------|-----------------------------|-----------|
| 5 | `operon-plugin brings claudechic-style multi-Agent orchestration (Agent` | comment/prose | EDITORIAL -- recommend dropping "claudechic-style"; rephrase the value prop in operon's own terms. Flag for the README-rework owner. | N |
| 24 | ``\`claudechic/.project_team/claude_code_port/SPEC_APPENDIX.md\` §F.`` | comment/prose (functional pointer) | depends on spec relocation (D9/D3) -- see "Spec-pointer" flag below | N |
| 290 | `The authoritative design lives in the claudechic repository at` | comment/prose (functional pointer) | depends on spec relocation -- see flag | N |
| 291 | ``\`../claudechic/.project_team/claude_code_port/SPEC.md\` and`` | comment/prose (functional pointer) | depends on spec relocation -- see flag | N |

### plugins/operon-plugin/workflows/project_team/README.md (1)

| line | current text | category | proposed operon replacement | Behavior? |
|------|--------------|----------|-----------------------------|-----------|
| 5 | `> Ported from claudechic's \`project_team\` workflow (Phase 11). Role names, phase ordering, and rule semantics mirror claudechic; see commit \`phase 11: project_team workflow port\` for adaptation details.` | comment/prose (user-facing blockquote) | DELETE the blockquote, or replace with an operon-native one-liner describing the workflow. High visibility. | N |

### src/operon_mcp_server/checks/builtins.py (3)

| line | current text | category | proposed operon replacement | Behavior? |
|------|--------------|----------|-----------------------------|-----------|
| 4 | ``\`re\`). Mirrors \`claudechic/checks/builtins.py\` per SPEC §11.2; the`` | comment/prose | drop "Mirrors \`claudechic/checks/builtins.py\`"; keep "per SPEC §11.2" if still accurate | N |
| 45 | ``Mirrors claudechic's helper. The order matters: \`~\` expansion FIRST`` | comment/prose | `The order matters: \`~\` expansion FIRST` | N |
| 359 | `Expected shape per claudechic's existing convention:` | comment/prose | `Expected shape:` | N |

### src/operon_mcp_server/checks/protocol.py (1)

| line | current text | category | proposed operon replacement | Behavior? |
|------|--------------|----------|-----------------------------|-----------|
| 3 | `Mirrors \`claudechic/checks/protocol.py\`. This module declares the` | comment/prose | `This module declares the` (drop the Mirrors sentence) | N |

### src/operon_mcp_server/tools/request_override.py (1)

| line | current text | category | proposed operon replacement | Behavior? |
|------|--------------|----------|-----------------------------|-----------|
| 28 | `Workers/Coordinator distinction is intentional per claudechic's` | comment/prose | `Workers/Coordinator distinction is intentional` (drop "per claudechic's ..." clause; verify line 29 continuation) | N |

### src/operon_mcp_server/tools/restore_operon_session.py (1)

| line | current text | category | proposed operon replacement | Behavior? |
|------|--------------|----------|-----------------------------|-----------|
| 31 | ``Two entry modes (matches the claudechic \`\`chicsessions.py\`\` /`` | comment/prose | `Two entry modes (...` -- drop "matches the claudechic \`chicsessions.py\`" parenthetical (verify line 32 continuation) | N |

### src/operon_mcp_server/bootstrap.py (1)

| line | current text | category | proposed operon replacement | Behavior? |
|------|--------------|----------|-----------------------------|-----------|
| 63 | `#: canonical values claudechic uses for its TUI's auto-spawned` | comment/prose | reword to state the canonical values without claudechic attribution (e.g. `#: canonical default values for the auto-spawned ...`) | N |

### CHANGELOG.md (1)

| line | current text | category | proposed operon replacement | Behavior? |
|------|--------------|----------|-----------------------------|-----------|
| 6 | ``\`../claudechic/.project_team/claude_code_port/SPEC.md\` +`` | comment/prose (functional pointer) | depends on spec relocation -- see flag. CHANGELOG is historical; relocating may be inappropriate -- flag for human. | N |

---

## Open flags (need a human / cross-agent decision)

### F1 -- Token removal is the ONLY behavior-affecting item
`workflow.py:438-441` -- removing the `${CLAUDECHIC_ARTIFACT_DIR}` alias
`.replace()`. No current template/test uses the alias spelling, so observable
behavior for shipped workflows is unchanged, but the function itself changes.
**Requires a regression test** (assert `${ARTIFACT_DIR}` still expands;
assert the old alias is no longer special-cased). All other 35 actionable
hits are pure text (N).

### F2 -- Spec-location pointers (README:24,290-291 + CHANGELOG:6)
These point to `../claudechic/.project_team/claude_code_port/SPEC.md` /
`SPEC_APPENDIX.md` as the AUTHORITATIVE spec. They are FUNCTIONAL pointers,
not attribution. Per STATUS decisions D3 (move audit docs out) and D9
(docs-site-is-spec), the authoritative spec location is changing. The
replacement text depends on where the spec lands -- coordinate with
composability (spec.md owner). Do NOT blindly delete; that orphans the reader.
CHANGELOG.md:6 is a historical entry -- rewriting history-doc pointers may be
inappropriate; flag for human.

### F3 -- README.md:5 positioning line ("claudechic-style")
Editorial: the README rework owner decides whether operon's value prop keeps
any reference to its origin or is stated purely in operon's own terms. The
full-removal directive argues for dropping "claudechic-style" entirely.

### F4 -- Internal audit docs (OPEN -- per lead's instruction, no replacements proposed)
These four docs under `docs/` exist specifically to DOCUMENT the
claudechic -> operon port (ground-truthing operon's design against claudechic
source). Their claudechic references are load-bearing to their purpose.

| doc | claudechic hits | purpose |
|-----|-----------------|---------|
| docs/SPEC_GROUNDING_AUDIT.md | 35 | per-row audit: which operon artifacts were ported from claudechic, cross-checked against claudechic source paths |
| docs/APPENDIX.MD | 10 | design appendix citing claudechic guardrails/hooks/rules as the porting baseline |
| docs/AGENT_TEAMS_PIVOT_PLAN.md | 8 | pivot plan referencing claudechic patterns being mirrored |
| docs/SCOPE_AUDIT_v2.1.md | 0 | (named by lead; no claudechic hits) |

STATUS.md "Key references" still lists three of these as "Internal-only docs
(leave as-is)", but decision D3 says MOVE the audit docs OUT of the published
tree. **Pending user decision:** (a) leave them (scrubbing destroys their
meaning), (b) move them out of `docs/` so they aren't published by MkDocs but
keep their content, or (c) scrub claudechic from them too (large effort,
arguably defeats their purpose). I propose NO replacements for these 55 hits
until the user resolves D3 vs the full-removal directive.

---

## Summary for remediation (Implementation phase)

- **Actionable now (35 hits, 11 files):** 34 pure-text edits (comments/
  docstrings/prose) + 1 behavior change (token alias removal, needs test).
- **Behavior-affecting: exactly 1** -- `workflow.py` token alias `.replace()`.
- **Deferred pending decisions:** F2 spec pointers (4 hits), F4 audit docs
  (55 hits).
- **Do NOT touch:** project_team.yaml:98 dual-match regex (live logic, no
  claudechic string); operon's real rule ids that coincidentally share
  claudechic names (warn_sudo, log_git_operations) -- drop only the "Mirrors
  claudechic" framing, keep the ids.
