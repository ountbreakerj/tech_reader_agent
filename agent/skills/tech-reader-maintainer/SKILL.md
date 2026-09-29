---
name: tech-reader-maintainer
description: Maintain the modular Tech Reader project from natural-language requests, editing only the smallest relevant source files and always building and validating the offline single-file deliverable.
metadata:
  short-description: Maintain Tech Reader modules safely
---

# Tech Reader Maintainer

Use this skill for requests to add, edit, reorganize, style, or repair content in the Tech Reader project.

## Required operating rules

- Work inside the project root and read `README.md` first.
- Treat `dist/tech-reader.html` as generated output. Treat `src/vendor/` as immutable third-party code unless the user explicitly scopes it.
- Use `catalog.json` when present; otherwise run `node tools/catalog.mjs` before broad exploration.
- Select the smallest affected module and files. Read the target content, its module configuration, relevant CSS, and linked design rules only.
- Never reconstruct the whole HTML file in a prompt or use a global regex rewrite for ordinary edits.
- Keep DOM IDs stable. If an ID changes, update the module TOC, `tech-points.json`, and all known internal links in the same change.
- Use `@asset/<id>` for images. Register new binary files through `node tools/assets.mjs add` and never paste large base64 payloads into source.

## Default maintenance workflow

1. Classify the request, inspect the smallest relevant source set, and record a reversible inventory before editing.
2. For structural, ambiguous, or underspecified requests, conduct a short structured interview before choosing an information architecture or changing files.
3. For large tasks, present the complete plan first and report progress after each step. For clear local fixes, proceed after the inventory.
4. Make the smallest source change, then synchronize module configuration, TOC, anchors, terminology, links, and assets that refer to it.
5. Run the validation and design-review gates below, followed by one final syntax/structure pass over the changed files and their registries.

## Cross-module content and typography contract

- When the request names a visual reference or asks for module-wide alignment, apply one consistent baseline to every page in the requested scope, including related subpages and linked technical entries.
- Preserve existing information architecture, navigation behavior, semantic hierarchy, and module identity colors unless a broader change is explicitly requested.
- Fixed body font: `-apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", "Noto Sans SC", sans-serif`; body size `15.5px`; body line-height `1.8`.
- Fixed heading sizes: `h1 26px`, `h2 20px`, `h3 16.5px`, `h4 15px`.
- Fixed code font: `"SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace`; code size `13px` on desktop and `11.5px` on mobile; line-height `1.6`/`1.5`.
- Fixed code block palette: background `#1e1e2e`, text `#cdd6f4`, border `#313244`, optional header background `#181825`.
- Keep long code and tables scrollable inside their own containers on narrow screens; never allow page-level horizontal overflow.
- Do not apply a fixed artifact-size limit unless the user explicitly requests one for the current task.
- Register external images with `@asset/<id>` and embed them in the generated offline artifact.
- Convert mathematical markup into readable offline HTML; raw formula delimiters must not be reader-facing.

## Decision priority and visual acceptance

- Resolve conflicts in this order: current explicit user decision, approved project rules, design system, PRD/history, existing implementation.
- For module-wide typography or code-background changes, inspect desktop, tablet, and mobile screenshots and resolve all unexplained `blocking` and normally all `major` design findings before delivery.

## Request routing

- Text or code example → target `content.html`.
- Local appearance → the same section's `style.css`.
- Shared appearance, navigation, theme, search, or layout → `src/shell/` and/or `shared.css`, with full visual review.
- New, moved, renamed, or removed section → target `module.json`, content, style, TOC, stable IDs/anchors, internal links, and any asset entries.
- New terminology → `src/data/tech-points.json` plus its target anchor.
- New illustration → image asset workflow, then content/CSS reference.
- Cross-computer release → `tools/package.ps1`, `一键部署.cmd`, and `deploy.ps1`; keep the ZIP self-contained and do not include machine-specific `.codegraph/` data.

## Verification gate

After every mutation:

1. Run `node build.mjs`.
2. Run `node verify.mjs`.
3. Run `node tools/assets.mjs audit` when assets or HTML/CSS references changed.
4. Run `node build.mjs --legacy --check-baseline --no-write` only when checking the import split or an explicitly requested byte-preserving migration.
5. For content additions/removals, interaction, image, or styling changes, use `tech-reader-design-review`; do not declare completion from code inspection alone.
6. Perform a final syntax and structure pass over changed Markdown/YAML/JSON/HTML/CSS/JS and the linked module registry.

Report the changed files, build SHA-256, validation result, and any design-review decision. If a blocking design finding remains, stop and ask for a decision instead of silently shipping it.
