---
name: audible-goodreads-deal-scout
description: Evaluate an Audible daily promotion against Goodreads public score, optional Goodreads CSV shelves, optional freeform reading notes, optional delivery rules, and manual Want-to-Read discount scans. Use for first-run setup, deal checks, scheduled sends, and on-demand Goodreads backlog audits.
license: MIT
metadata: {"openclaw":{"emoji":"🎧","skillKey":"audible-goodreads-deal-scout","homepage":"https://github.com/lenpr/audible-goodreads-deal-scout","category":"media","requires":{"anyBins":["python3","python"]}}}
---

# Audible Goodreads Deal Scout

Use this skill when the user wants to:
- set up a reusable Audible deal workflow
- check the current Audible daily promotion
- scan Goodreads Want-to-Read books for visible Audible US discounts
- personalize the result with a Goodreads CSV and/or freeform notes
- finalize and optionally deliver the result into a configured channel

## Runtime output contract

The skill runtime must return JSON only in this shape:

```json
{
  "schemaVersion": 1,
  "goodreads": {
    "status": "resolved | no_match | lookup_failed",
    "url": "string | null",
    "title": "string | null",
    "author": "string | null",
    "averageRating": "number | null",
    "ratingsCount": "integer | null",
    "evidence": "string | null"
  },
  "fit": {
    "status": "written | not_applicable | unavailable",
    "sentence": "string | null"
  }
}
```

## Use the Python prep layer first

Do not fetch Audible yourself in model text. Always start with the prep layer:

```bash
sh "{baseDir}/scripts/audible-goodreads-deal-scout.sh" prepare --config-path "<config-path>" --invocation-mode manual
```

Prep returns JSON with:
- `status: ready | suppress | error`
- `reasonCode`
- `warnings[]`
- `audible`
- `personalData`
- `artifacts`
- `metadata`

If prep returns `suppress` or `error`, surface that result directly and stop. Do not do Goodreads lookup or fit writing after a prep-layer short-circuit.

## Setup

If the skill is not configured yet, gather:
1. Audible store
2. Whether the user wants personalization
3. Optional Goodreads CSV path
4. Optional notes file or pasted notes
5. Optional threshold override from the default `3.8`
6. Optional delivery channel and target
7. Delivery policy: `positive_only`, `always_full`, or `summary_on_non_match`
8. Whether daily automation should be enabled

Then write config through:

- If the user does not request a custom path, use the default workspace-root storage path: `<workspace>/.audible-goodreads-deal-scout/`.
- Do not invent legacy names like `.audible-goodreads-deal`.
- Do not store mutable config, state, or artifacts inside `{baseDir}` or the installed skill folder. `openclaw skills install` and `openclaw skills update --force` replace the workspace skill directory.

```bash
sh "{baseDir}/scripts/audible-goodreads-deal-scout.sh" setup \
  --config-path "<config-path>" \
  --audible-marketplace "<marketplace>" \
  --threshold "<threshold>" \
  [--goodreads-csv "<csv-path>"] \
  [--notes-file "<notes-file>"] \
  [--notes-text "<inline notes>"] \
  [--delivery-channel "telegram"] \
  [--delivery-target "<target>"] \
  [--delivery-policy "positive_only"] \
  [--daily-automation] \
  [--register-cron]
```

Use interactive `setup` only when the user explicitly wants prompt-by-prompt CLI onboarding. Otherwise prefer the non-interactive command with concrete flags.

## Want-to-Read discount scan

Use this only when the user asks to scan Goodreads Want-to-Read books for Audible discounts. This is a manual audit command, not a cron or delivery workflow.

Requirements:
- The configured Goodreads CSV must exist.
- V1 supports Audible US only.
- Do not create cron jobs or send delivery messages for this command.
- Do not perform extra model web searches; the Python command handles Audible lookup and report generation.

Run:

```bash
sh "{baseDir}/scripts/audible-goodreads-deal-scout.sh" scan-want-to-read \
  --config-path "<config-path>" \
  [--audible-auth-path "<auth-path>"] \
  [--limit 40] \
  [--offset 0] \
  [--scan-order newest] \
  [--output-json "<json-path>"] \
  [--output-md "<markdown-path>"]
```

Default behavior:
- Print compact Markdown to stdout.
- Show visible numeric discounts first.
- Suppress long non-deal lists unless `--include-non-deals` is requested.
- Use `--offset` and `--limit` for large Goodreads backlogs.

Important caveat: Audible often hides cash prices behind credit or membership UI. Treat `price_hidden`, `price_unknown`, and `needs_review` as honest uncertainty, not as failures.

Optional authenticated pricing:
- If the user asks for headless authenticated Audible prices, use `audible-auth-start` and `audible-auth-finish`.
- Do not ask for the user's Audible or Amazon password.
- Store the auth file under the workspace storage directory, for example `<workspace>/.audible-goodreads-deal-scout/audible-auth.json`.
- Treat the auth file as sensitive and never paste its token contents into chat.
- Authenticated scans usually spend one search request plus one authenticated price request for each matched title; set `--max-requests` accordingly.
- Treat cash pricing fields as the source of truth and do not classify Audible credit prices, including `credit_price`, as cash discounts.

```bash
sh "{baseDir}/scripts/audible-goodreads-deal-scout.sh" audible-auth-start \
  --auth-path "<workspace>/.audible-goodreads-deal-scout/audible-auth.json" \
  --audible-marketplace us

sh "{baseDir}/scripts/audible-goodreads-deal-scout.sh" audible-auth-finish \
  --auth-path "<workspace>/.audible-goodreads-deal-scout/audible-auth.json" \
  --redirect-url "<final-amazon-redirect-url>"
```

## Ready flow

For `ready_*` prep results:
1. Read `artifacts.runtimePromptPath`
2. Read `artifacts.runtimeInputPath`
3. Resolve the Goodreads public book page and score with OpenClaw web/search
4. Produce JSON only that matches `artifacts.runtimeOutputSchemaPath`
5. Finalize through:

```bash
sh "{baseDir}/scripts/audible-goodreads-deal-scout.sh" finalize \
  --prepare-json "<prepare-result-path>" \
  --runtime-output "<runtime-output-path>"
```

If the user wants the result routed to a configured channel:

```bash
sh "{baseDir}/scripts/audible-goodreads-deal-scout.sh" run-and-deliver \
  --config-path "<config-path>" \
  --prepare-json "<prepare-result-path>" \
  --runtime-output "<runtime-output-path>"
```

## Decision rules

- If `personalData.exactShelfMatch == "to-read"`, recommend immediately. This overrides the Goodreads threshold.
- If prep already marked the book as `read` or `currently-reading`, do not continue.
- Otherwise enforce the Goodreads threshold from `metadata.threshold`.
- If Goodreads lookup fails, use `error_goodreads_lookup_failed`.
- If Goodreads cannot confirm a matching book page, use `suppress_no_goodreads_match`.

Skill-layer reason codes:
- `recommend_to_read_override`
- `recommend_public_threshold`
- `suppress_below_goodreads_threshold`
- `suppress_no_goodreads_match`
- `error_goodreads_lookup_failed`

## Fit writing

The model writes the fit paragraph. Python does not call a provider API directly.

Use:
- Goodreads public score
- `artifacts.fitContextPath`
- `artifacts.reviewSourcePath` when present
- `artifacts.notesPath` when present

Rules:
- Do not drop rated/reviewed CSV rows for prompt convenience.
- Summarize each written Goodreads review to 500 characters or fewer before using it as evidence.
- If `personalData.privacyMode == "minimal"`, do not use personal CSV or notes content in the fit paragraph.
- If no meaningful personal data exists, say so explicitly instead of inventing taste evidence.

Fallback lines:
- `Fit: No personal preference data was configured, so this recommendation is based only on the public Goodreads score.`
- `Fit: Personalized fit feedback is unavailable right now, but the recommendation decision still completed.`

## Delivery

`run-and-deliver` must respect the configured `deliveryPolicy`:
- `positive_only`: deliver only `recommend`
- `always_full`: deliver the full card for every final status
- `summary_on_non_match`: deliver full `recommend`, but a short summary card for `suppress` or `error`

For scheduled runs, prep with `--invocation-mode scheduled`. If prep returns `suppress_duplicate_scheduled_run`, stop quietly. After a surfaced scheduled result, mark the deal as emitted with:

```bash
sh "{baseDir}/scripts/audible-goodreads-deal-scout.sh" mark-emitted --state-file "<state-file>" --deal-key "<deal-key>"
```
