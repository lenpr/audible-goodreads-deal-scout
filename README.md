# Audible Goodreads Deal Scout

`audible-goodreads-deal-scout` evaluates an Audible daily promotion against:
- Goodreads public score
- optional Goodreads CSV shelves/history
- optional freeform reading notes
- optional delivery rules for Telegram or another configured channel

It is designed as an OpenClaw skill first, and this repository is the clean standalone version that can be published to [ClawHub](https://clawhub.ai).

## Repository layout

- `SKILL.md`: agent-facing instructions and runtime contract
- `audible_goodreads_deal_scout/core.py`: prep/orchestration logic
- `audible_goodreads_deal_scout/rendering.py`: card rendering and delivery planning
- `audible_goodreads_deal_scout/delivery.py`: config, cron, and delivery helpers
- `audible_goodreads_deal_scout/public_cli.py`: setup and CLI entrypoint
- `config.example.json`: public config example
- `agents/openai.yaml`: UI metadata for OpenClaw
- `tests/test_audible_goodreads_deal_scout.py`: skill-specific test suite

## Quick start

From the repository root:

```bash
python3 -m audible_goodreads_deal_scout.public_cli setup
```

Non-interactive example:

```bash
python3 -m audible_goodreads_deal_scout.public_cli setup \
  --non-interactive \
  --config-path .audible-goodreads-deal-scout/config.json \
  --audible-marketplace us \
  --threshold 3.8 \
  --goodreads-csv "/absolute/path/to/goodreads_library_export.csv" \
  --notes-file "/absolute/path/to/preferences.md" \
  --delivery-channel telegram \
  --delivery-target "-1000000000000" \
  --delivery-policy positive_only
```

Prepare the current deal:

```bash
python3 -m audible_goodreads_deal_scout.public_cli prepare \
  --config-path .audible-goodreads-deal-scout/config.json
```

Useful helpers:

```bash
python3 -m audible_goodreads_deal_scout.public_cli show-csv-headers "/absolute/path/to/goodreads_library_export.csv"
python3 -m audible_goodreads_deal_scout.public_cli measure-context --goodreads-csv "/absolute/path/to/goodreads_library_export.csv" --output /tmp/fit-context.json
python3 -m audible_goodreads_deal_scout.public_cli publish-audit --version 0.1.0
```

Finalize a runtime Goodreads result:

```bash
python3 -m audible_goodreads_deal_scout.public_cli finalize \
  --prepare-json .audible-goodreads-deal-scout/artifacts/current/prepare-result.json \
  --runtime-output /tmp/runtime-output.json
```

Finalize and deliver in one step:

```bash
python3 -m audible_goodreads_deal_scout.public_cli run-and-deliver \
  --config-path .audible-goodreads-deal-scout/config.json \
  --prepare-json .audible-goodreads-deal-scout/artifacts/current/prepare-result.json \
  --runtime-output /tmp/runtime-output.json
```

## Delivery policy

`deliveryPolicy` controls what gets sent:
- `positive_only`: only deliver `recommend` results
- `always_full`: deliver the full card for `recommend`, `suppress`, and `error`
- `summary_on_non_match`: deliver full `recommend`, but shorter cards for `suppress` and `error`

Default public behavior should stay `positive_only`.

## Publish to ClawHub

Official publish flow:

```bash
clawhub login
clawhub skill publish . \
  --slug audible-goodreads-deal-scout \
  --name "Audible Goodreads Deal Scout" \
  --version 0.1.0 \
  --changelog "Initial public release" \
  --tags latest
```

Before publishing, run:

```bash
python3 -m audible_goodreads_deal_scout.public_cli publish-audit --version 0.1.0 --tags latest
```

## Why this project is useful

The value is not just “fetch today’s Audible deal.” It is:
- filtering instead of spam
- a clean split between public quality signal and personal taste signal
- optional delivery into a real channel
- graceful suppression and failure handling
