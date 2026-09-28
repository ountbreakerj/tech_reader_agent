---
name: tech-reader-design-review
description: Audit Tech Reader changes for product intent, UX interaction, UI consistency, visual quality, accessibility, and asset fit using evidence from the built artifact and targeted screenshots.
metadata:
  short-description: Audit Tech Reader design and interaction
---

# Tech Reader Design Review

Use this skill after a change that adds/removes content, changes interaction, adds assets, or changes local/global styling. It is an independent audit gate for the maintainer Agent.

## Inputs

- The user request or acceptance criteria.
- Changed-file list and `node verify.mjs` output.
- Targeted screenshots from `node tools/capture-visuals.mjs`; use desktop/tablet/mobile for responsive changes.
- `design/design-system.md`, `design/product-principles.md`, and `design/interaction-rules.md`.

## Review lenses

- Product: scope, information architecture, wording, user goal, edge cases.
- UX: navigation, feedback, focus, keyboard/touch equivalence, error/empty/loading states, reversibility.
- UI: hierarchy, spacing, typography, contrast, responsive behavior, theme consistency.
- Art: asset clarity, crop, visual language, relevance, alt text, performance impact.
- Accessibility: semantic structure, labels, focus visibility, contrast, reduced-motion considerations.

## Procedure

1. Inspect only the affected route/module and its screenshots unless the change is global.
2. Separate evidence from preference. A finding must name its location, observed behavior, expected behavior, and actionable recommendation.
3. Mark `blocking` only for broken requirements, inaccessible content, broken navigation, unusable interaction, or severe visual failure. Use `major`, `minor`, and `suggestion` for lower impact issues.
4. Write a report conforming to `references/review-schema.md` or validate an existing report with `node tools/review.mjs <report.json>`.
5. The maintainer Agent must fix all `blocking` and normally all `major` findings, then rerun only the necessary checks. Permit at most two review/fix rounds before escalating a remaining disagreement to the user.

Do not replace a deterministic test with an aesthetic opinion. Do not approve a change solely because the source looks tidy; judge the rendered artifact and the requested user outcome.
