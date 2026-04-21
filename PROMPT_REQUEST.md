# Prompt Request

Use this file if you want to re-implement the skill's behavior without using the code in this repository directly.

The opening sections are written for a human reader first. The later sections tighten the contracts enough that a coding agent can rebuild something very close to the current skill behavior.

## Intention

Build a workflow that evaluates the current Audible daily promotion and answers one practical question for a real reader:

Is this deal worth this user's attention?

This should not feel like a deal feed. It should feel like a selective filter for someone who wants fewer, better recommendations.

The workflow should combine a public quality signal with optional personal context so the output feels concise, grounded, and useful rather than noisy or generic.

## Scope

The workflow should:
- fetch the current Audible daily promotion from a chosen marketplace
- identify the promoted book and its basic metadata
- look up Goodreads signal for the same title
- optionally use a Goodreads library export CSV to detect whether the user already read the book or already saved it to `to-read`
- optionally use freeform reading notes to write a short fit judgment
- optionally apply a delivery policy that decides whether to send the result or suppress it quietly

The workflow should not:
- behave like a generic Audible scraper
- turn into a broad book recommender
- emit long essays
- depend on hidden internal infrastructure

## Implementation Model

To match this skill closely, implement it as a staged workflow rather than one monolithic prompt.

1. Prepare step:
   Fetch the Audible promotion deterministically, parse the book metadata, load optional CSV and notes inputs, apply local short-circuit rules, and write machine-readable artifacts.
2. Runtime step:
   Consume the prepared artifacts, resolve Goodreads public data, and write a short fit judgment when meaningful personalization is available. Return JSON only.
3. Finalize step:
   Combine the prepare result and runtime JSON into the final recommendation, suppression, or error result and render the user-facing message.
4. Optional delivery step:
   Send or skip the final message depending on the configured delivery policy.

The core design choice is that deterministic work should stay deterministic. The model step should be limited to Goodreads resolution and compact fit writing, not raw page scraping or basic control flow.

## Required Inputs

The minimum useful inputs are:
- Audible marketplace, for example `us`, `uk`, `de`, `ca`, or `au`
- a Goodreads score threshold, with `3.8` as the default baseline

Optional inputs:
- a Goodreads library export CSV
- freeform reading notes
- a freshness window for warning about stale Goodreads exports
- a privacy mode
- an invocation mode such as `manual` or `scheduled`
- a state file for duplicate scheduled suppression
- an artifact directory for intermediate outputs
- a delivery policy
- a delivery channel and target

## Decision Rules

Apply these rules in order:

1. Fetch the current Audible daily promotion first.
2. If the promotion cannot be fetched or parsed cleanly, return a structured error or suppression and stop.
3. If the user explicitly supplied a notes file and it is missing, return an explicit error instead of silently degrading.
4. If the user explicitly supplied a Goodreads CSV and it cannot be read, return an explicit error instead of silently degrading.
5. If CSV column overrides are provided, validate them against the real CSV headers before parsing the rows.
6. If Goodreads CSV matching is ambiguous for the same title, return an explicit error instead of guessing.
7. If the book is already marked as `read`, suppress the recommendation before Goodreads lookup.
8. If the book is already marked as `currently-reading`, suppress the recommendation before Goodreads lookup.
9. If the run is scheduled and the same deal was already emitted, suppress duplicate delivery.
10. If a `to-read` exact shelf match exists, keep the run active. That shelf state is positive evidence and should be able to override the public threshold later.
11. Resolve Goodreads data for the promoted title.
12. Treat Goodreads status as one of:
    - `resolved`
    - `no_match`
    - `lookup_failed`
13. If Goodreads status is `lookup_failed`, the final result should be an error.
14. If Goodreads status is `no_match`, the final result should be a suppression.
15. If Goodreads rating is resolved and less than or equal to the threshold, suppress the recommendation.
16. If Goodreads rating is resolved and greater than the threshold, recommend the book.
17. If notes or Goodreads history are available, write one short fit paragraph.
18. If fit writing is unavailable but the decision can still be made, keep the decision and use a fallback fit line instead of leaving fit empty.
19. Keep the final result concise and decision-oriented.

## Stable Contracts

To reproduce this skill closely, preserve the distinction between prepare-time short-circuits and finalize-time Goodreads decisions.

Prepare step statuses:
- `ready`
- `suppress`
- `error`

Common prepare reason codes:
- `ready_public`
- `ready_notes`
- `ready_full`
- `suppress_no_active_promotion`
- `suppress_duplicate_scheduled_run`
- `suppress_already_read`
- `suppress_currently_reading`
- `error_missing_notes_file`
- `error_missing_csv`
- `error_csv_unreadable`
- `error_ambiguous_personal_match`
- `error_audible_blocked`
- `error_audible_fetch_failed`
- `error_audible_parse_failed`

Final step statuses:
- `recommend`
- `suppress`
- `error`

Common final reason codes:
- `recommend_to_read_override`
- `recommend_public_threshold`
- `suppress_below_goodreads_threshold`
- `suppress_no_goodreads_match`
- `error_goodreads_lookup_failed`

The exact string set can expand, but those distinctions should remain intact.

## Output Shape

The workflow should produce three structured contracts.

Prepare result:
- `schemaVersion`
- `status`
- `reasonCode`
- `warnings`
- `audible`
- `personalData`
- `artifacts`
- `metadata`
- `message`

Runtime output:

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

Final result:
- `schemaVersion`
- `status`
- `reasonCode`
- `reasonText`
- `warnings`
- `audible`
- `goodreads`
- `fitSentence`
- `metadata`
- `message`

The runtime schema should be validated strictly:
- `schemaVersion` must be `1`
- `goodreads.status` must be `resolved`, `no_match`, or `lookup_failed`
- `fit.status` must be `written`, `not_applicable`, or `unavailable`
- when `fit.status` is `written`, `fit.sentence` must be non-empty
- when Goodreads status is not `resolved`, rating and URL fields should not be populated as if a match were confirmed

The final user-facing message should communicate:
- what the book is
- whether Goodreads signal clears the quality bar
- whether there is personal match evidence
- one likely appeal
- one credible downside when fit text is written
- whether the book was already read or already on `to-read`, when known

## Guardrails

- Do not ask the model to freestyle raw page scraping when a deterministic prep layer or parser can do it first.
- Do not silently ignore malformed or missing personal-data inputs when the user explicitly supplied them.
- Keep exact shelf logic local and deterministic rather than model-decided.
- Do not rely on prompt obedience alone for privacy; use data minimization.
- In minimal privacy mode, do not write or expose personal fit artifacts to the model runtime.
- In minimal privacy mode, shelf-state logic may still be used locally, but the model should receive only summary metadata, not raw notes or detailed CSV taste artifacts.
- Goodreads matching should be conservative. Prefer `no_match` or `lookup_failed` over guessing.
- Do not collapse `lookup_failed` and `no_match` into the same outcome.
- Keep wording polished and short. This is a filter, not a review blog.
- Keep example paths, IDs, and configuration values generic and public-safe.
- Keep publishable bundles free of local state, generated artifacts, tests, and private machine details.

## Edge Cases And Design Choices

- Notes-only mode is valid. The workflow should still run without a Goodreads CSV.
- CSV-only mode is valid. The workflow should still run without notes.
- Public-signal-only mode is valid. The workflow should still run with neither CSV nor notes.
- Missing notes files should raise an explicit error or warning, not silently degrade personalization.
- Missing CSV files should fail explicitly when the user explicitly configured them.
- CSV column overrides must match real CSV headers exactly; invalid overrides should fail clearly.
- Stale Goodreads exports should warn, not hard fail, because old data is still often better than no data.
- Goodreads lookup failure should not be confused with "no match." Those are different conditions and should stay distinguishable.
- A minimal privacy setting should reduce exposed personal context materially, not just add a textual instruction.
- A `to-read` exact shelf match is not a suppression. It is positive evidence and should be able to override the public threshold in the final decision.
- `read` and `currently-reading` exact shelf matches are short-circuit suppressions and should happen before Goodreads lookup.
- Scheduled duplicate suppression should rely on persisted state and should not block manual runs.
- If the model cannot write a fit paragraph, the workflow should still finalize with a fallback fit message rather than fail the whole run.
- Delivery policy should be explicit:
  - `positive_only`: send only positive recommendations
  - `always_full`: always send the full result
  - `summary_on_non_match`: send a shortened summary for suppress cases

## Artifact Expectations

The prepare step should persist machine-readable artifacts so the runtime and finalize steps can be run separately.

At minimum, keep equivalents of:
- the prepared Audible deal data
- summary personal-data metadata
- the runtime input JSON
- the runtime task instructions
- the runtime output schema
- the saved prepare result

When personalization is allowed, also persist equivalents of:
- a compact CSV fit-context artifact
- a review-source artifact
- the notes text artifact

When `privacyMode` is `minimal`, those detailed personal artifacts should be omitted.

## Example Prompt

Use this as a starting point if you want to recreate the workflow with a different stack:

```text
Implement a staged workflow that evaluates the current Audible daily deal for a user.

Inputs:
- Audible marketplace
- Goodreads score threshold (default 3.8)
- optional Goodreads library export CSV
- optional freeform reading notes
- optional privacy mode
- optional invocation mode (manual or scheduled)
- optional state file for duplicate scheduled suppression
- optional artifact directory
- optional delivery policy

Requirements:
- Split the workflow into prepare, runtime, finalize, and optional delivery stages.
- In prepare, fetch and parse the current Audible daily promotion deterministically.
- Identify title, author, price, runtime, sale timing, marketplace, and Audible URL.
- In prepare, load optional CSV and notes inputs and validate explicit file paths and CSV header overrides.
- In prepare, classify exact shelf matches. Suppress read and currently-reading before Goodreads lookup. Keep to-read as positive evidence.
- In prepare, suppress duplicate scheduled emissions for the same deal using persisted state, but do not let that suppress manual runs.
- In runtime, look up Goodreads status for the same book and classify it as resolved, no_match, or lookup_failed.
- In runtime, return JSON only and obey this exact schema:
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
- In runtime, if privacyMode is minimal, do not use raw personal CSV or notes artifacts.
- In finalize, map Goodreads lookup_failed to an error, no_match to a suppression, score <= threshold to a suppression, and score > threshold to a recommendation.
- In finalize, let an exact to-read shelf match override the public threshold and produce a recommendation.
- If the model cannot write fit text reliably, keep the decision and use a fallback fit line instead of failing the whole run.
- Keep the final message concise, polished, and decision-oriented.

Return structured machine-readable prepare/runtime/final results plus a short user-facing message.
```

## Limitations

- This document captures the intended behavior, not every implementation detail.
- Goodreads matching is inherently imperfect and may require fallback logic.
- Public rating thresholds are useful heuristics, not truth.
- Freeform notes can improve fit quality, but weak notes produce weak fit output.
- A reimplementation should still add its own tests, privacy checks, and publish hygiene.
