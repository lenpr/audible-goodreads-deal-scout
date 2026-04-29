# Changelog

## 0.1.9

- Add a human-facing ClawHub overview to `SKILL.md` so the marketplace README tab explains the skill before agent runtime instructions
- Keep agent-facing operational instructions under a dedicated runtime section
- Update release metadata defaults for the marketplace-rendering fix

## 0.1.8

- Refresh ClawHub README and release metadata

## 0.1.7

- Add stderr progress reporting for long Want-to-Read scans, including JSONL mode for OpenClaw agents and log processors
- Suppress duplicate Audible product matches in final Want-to-Read reports while preserving scanned-row counts and duplicate metadata in JSON
- Add scan report metadata for authenticated-pricing state, cache stats, request usage, and next-batch hints
- Add `priceBasis` and `dealType` fields so reports can distinguish member cash prices below list from limited-time sale or promotion signals
- Enrich missing Goodreads average ratings for a capped number of discounted Want-to-Read results by Goodreads book id
- Retry transient daily-promotion fetch failures and temporary no-active-promotion parses before suppressing or erroring
- Add setup `nextSteps` guidance and strengthen publish-audit checks for generated/private artifact exclusions
- Clarify that authenticated `discounted` means member-visible cash price below list price, not guaranteed limited-time sale status
- Improve release and troubleshooting docs for long scans, authenticated price lookup, and local readiness checks
- Reduce duplicated scan-progress bookkeeping in the Want-to-Read scan implementation

## 0.1.5

- Add stdlib-only headless Audible external-login auth helpers for optional authenticated price lookup
- Add authenticated Audible catalog product pricing lookup for matched Want-to-Read scan results
- Add `--audible-auth-path` to `scan-want-to-read` and helper commands for auth start, finish, and one-ASIN price testing
- Add `doctor` and `audible-auth-status` commands for local readiness, auth expiry, and file-permission checks
- Add structured CLI error payloads with token redaction for command failures
- Ignore Audible credit prices when classifying authenticated cash discounts
- Clarify external-browser auth instructions and authenticated request budgeting
- Move prepare-result artifact writing behind the runtime contract module boundary
- Keep auth files out of published bundles and document that the auth file is sensitive local state

## 0.1.4

- Add a manual `scan-want-to-read` command for checking Goodreads `to-read` books against visible Audible US numeric discounts
- Add conservative Audible catalog search, pricing parsing, parsed-result cache, request budgeting, and block-like circuit breaker handling for the new scan
- Add compact Markdown and structured JSON reports for Want-to-Read discount scans
- Add offline fixture coverage for scan selection, matching, pricing, request budget behavior, cached block failures, and compact report output

## 0.1.3

- Invoke the published shell wrapper through `sh` in `SKILL.md` and README examples so official OpenClaw installs still work even when the installed script loses its executable bit
- Clarify that default config, state, and artifact storage belongs under `.audible-goodreads-deal-scout/` in the active OpenClaw workspace, not inside the installed skill folder or under the legacy `.audible-goodreads-deal` name
- Clarify in the README that configured CSV, notes, config, and state paths should only point at files the user intends the skill to read or write
- Add README guidance to review delivery settings before enabling daily automation or cron registration
- Soften transaction-heavy README wording for a skill that evaluates promotions rather than making purchases
- Add a release-check step to verify the live ClawHub license summary matches `SKILL.md` and `LICENSE.txt`

## 0.1.2

- Rename the published shell wrapper to `scripts/audible-goodreads-deal-scout.sh` so the ClawHub bundle exposes the documented entrypoint
- Rename the repository license file to `LICENSE.txt` and require it in the publish audit so extensionless files do not disappear from published bundles
- Update release guidance to verify the published wrapper and license files after upload

## 0.1.1

- Move the published wrapper entrypoint from `bin/` to `scripts/` so the package layout matches ClawHub skill conventions
- Update SKILL and README command examples to use the published `scripts/audible-goodreads-deal-scout` wrapper path
- Add release guidance to verify the published file manifest after upload

## 0.1.0

- Initial public release candidate for `audible-goodreads-deal-scout`
- Audible daily-promotion evaluation with Goodreads public score
- Optional Goodreads CSV shelf logic
- Optional freeform taste notes
- Delivery policy support for positive-only, full, and summary delivery
- Publish-time privacy audit
- Marketplace certification fixtures and runtime-contract tests
