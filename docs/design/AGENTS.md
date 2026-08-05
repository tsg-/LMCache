# Design-doc authoring rules

Read this before writing or heavily editing anything under `docs/design/`.
These extend the global "Technical Plans & Design Docs" rules with the
specific pathologies this tree has hit before.

## The five load-bearing rules (from global CLAUDE.md)

- Principal engineer with opinions, not policy generator.
- Detail by risk and uncertainty. No forced parallelism.
- State each caveat exactly once. No mirror sections.
- Primitives stay literal in specs; drift is fine in prose.
- Headings-only sweep before submitting anything over 500 lines.

## Elaborations specific to this repo

**Non-claims belong inline.** What a deliverable does NOT prove goes
into the exit-criteria sentence, not into a labeled "Explicit
non-claims:" bullet or a separate risk row. Say "D1 exit is a
reproducible remote-NVMe baseline; durability and crash recovery
are D2." Not: "**Explicit non-claims:** no durable LMCache cache
semantics, no crash recovery, no host-CPU baseline …"

**No duplicate tables.** One canonical stage / deliverable / test
description. If a second view is useful (by owner instead of by
stage), make it a projection with links, not a restatement. The
current plan's §7 delivery table and Appendix B stage runbook are
the canonical example of what not to do.

**Open questions with owner and date, or answer them.** An open
question without an owner and a target decision date is either
a real gap (add the owner) or laziness (answer it now).

**Rank risks; delete the ones you don't worry about.** A CYA risk
register with nine equal-weighted rows tells the reader nothing.
Two rows you actually worry about, ranked, plus a one-line
"routine operational risks are handled in the runbook" is better
than nine bullets no one reads.

**Ban:** "it is important to note," "it should be noted," the
"Explicit non-claims:" header pattern.

**Executive summary shape:** recommendation, delivery scope,
principal risks, decisions needed. One screen, actionable without
scrolling. Not a compressed restatement of the body.

## The heading sweep, spelled out

Before asking anyone to review a plan doc:

1. Run `grep -n "^##\|^###" <path>` and read the outline top to bottom.
2. If two headings promise the same content (outcomes, decision
   criteria, success gates), the doc has a mirror-section problem.
   Fix before you ask for review.
3. If an appendix restates something from the body rather than
   adding depth another plan will consume, merge or delete.
4. If §7 and Appendix B both describe the same stages with
   different words, one is canonical and the other links.
