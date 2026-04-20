# Audible Goodreads Deal Scout

`audible-goodreads-deal-scout` helps you decide whether an Audible daily promotion is worth your attention.

Instead of just showing today’s deal, it tries to answer:
- Is this book broadly well regarded on Goodreads?
- Have I already read it, or is it already on my `to-read` shelf?
- Based on my notes and reading history, does this feel like a strong fit or a bad fit?
- Should this be delivered to me, or quietly skipped?

## What you get out of it

There are three levels of value:

1. **Public signal only**
- No Goodreads CSV
- No notes
- You still get a basic recommendation based on the Goodreads public rating

2. **Taste notes**
- Add a short notes file describing what you like and dislike
- You still get the Goodreads public check, but now the fit paragraph becomes more personal

3. **Full value**
- Add your Goodreads library export CSV
- Optionally add personal notes too
- The skill can now suppress books you already read, fast-track books on your `to-read` shelf, and write a much better fit paragraph

If you want the best result, use both:
- a Goodreads CSV
- a short notes file

## How the recommendation works

At a high level:
- the skill fetches the current Audible daily promotion
- it finds the matching Goodreads book page and rating
- it compares the book against your Goodreads CSV if you provided one
- it optionally uses your notes to shape the fit paragraph
- it decides whether to deliver the result based on your delivery policy

### Goodreads threshold

The default threshold is `3.8`.

That means:
- a Goodreads average rating of `3.80` or lower is treated as below your quality cutoff
- a rating above `3.8` is eligible

This is the **public Goodreads average rating**, not your personal rating.

If you want to be stricter:
- try `4.0`

If you want more deals to pass through:
- try `3.6` or `3.7`

### Shelf rules

If you provide a Goodreads CSV:
- `read` => suppress
- `currently-reading` => suppress
- `to-read` => recommend immediately

That `to-read` override is intentional. If you already saved the book for later, that is treated as a strong positive signal.

## Supported Audible stores

Right now the code supports these marketplace keys:
- `us`
- `uk`
- `de`
- `ca`
- `au`

If you do nothing, the default is `us`.

When you set `audibleMarketplace`, use one of those short keys. Example:

```json
{
  "audibleMarketplace": "uk"
}
```

## What a notes file can look like

A notes file does **not** need a strict format. It can just be freeform text.

Example: [examples/preferences.example.md](/Users/D068138/Library/Mobile%20Documents/com~apple~CloudDocs/Coding/Codex/audible-goodreads-deal-scout/examples/preferences.example.md)

You can write things like:
- authors you often like
- genres you usually avoid
- whether you prefer literary fiction, thrillers, sci-fi, memoir, etc.
- what kinds of books you find too slow, too sentimental, too formulaic, too commercial

It is fine if the file is a bit messy. The point is to give the model a usable taste profile.

## What the output looks like

### Positive recommendation

```text
Audible US Daily Promotion — 2026-04-20

𝗦𝗹𝗮𝘂𝗴𝗵𝘁𝗲𝗿𝗵𝗼𝘂𝘀𝗲-𝗙𝗶𝘃𝗲 — Kurt Vonnegut (2015)
Price: $1.99 (-87%, list price $14.95)
Goodreads rating: 4.11 (1,364,737 ratings)
Length: 5:13 hrs
Genre: Literature & Fiction, Thought-Provoking, Fiction, Witty

Slaughterhouse-Five is the now famous parable of Billy Pilgrim...

Fit: Strong match, on your to-read shelf. You tend to respond well to fiction that is intellectually playful, structurally bold, and willing to smuggle serious ideas through wit. The main risk is that Vonnegut's emotional distance and satirical flatness may leave you admiring it more than fully feeling it.

Audible: https://www.audible.com/pd/...
Goodreads: https://www.goodreads.com/book/show/...
```

### Short non-match summary

```text
Audible US Daily Promotion — 2026-04-20

𝗦𝗹𝗮𝘂𝗴𝗵𝘁𝗲𝗿𝗵𝗼𝘂𝘀𝗲-𝗙𝗶𝘃𝗲 — Kurt Vonnegut (2015)

Fit: You marked it as read on Goodreads.

Audible: https://www.audible.com/pd/...
```

## Delivery behavior

You can use the skill manually in chat, or wire it into a delivery channel.

`deliveryPolicy` controls what gets sent:
- `positive_only`: only send likely fits
- `always_full`: send every evaluated result in full
- `summary_on_non_match`: send full recommendations, but short summary cards for suppressions/errors

Default recommendation:
- use `positive_only`

That gives the best signal-to-noise ratio for most people.

## Telegram, WhatsApp, and other channels

This repository does **not** bundle its own Telegram or WhatsApp connector.

Instead, it uses the OpenClaw message surface:
- `openclaw message send --channel ... --target ...`

So delivery works whenever your OpenClaw environment already has a supported channel configured.

### Telegram

If your OpenClaw setup supports Telegram delivery, configure:

```json
{
  "deliveryChannel": "telegram",
  "deliveryTarget": "-1000000000000"
}
```

`deliveryTarget` is the Telegram chat/channel id you want to post into.

### WhatsApp

WhatsApp can work **only if** your OpenClaw install already exposes a WhatsApp-capable message channel.

In that case, the pattern is the same:

```json
{
  "deliveryChannel": "whatsapp",
  "deliveryTarget": "<your-whatsapp-target>"
}
```

But this repo does not itself create or ship that WhatsApp channel. It only calls the channel if your OpenClaw runtime provides it.

## Quick start

From the repository root:

```bash
python3 -m audible_goodreads_deal_scout.public_cli setup
```

If you want a scripted setup instead:

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

## Core commands

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

## Files in this repository

- `SKILL.md`: agent-facing runtime instructions
- `audible_goodreads_deal_scout/core.py`: prep/orchestration logic
- `audible_goodreads_deal_scout/rendering.py`: card rendering and delivery planning
- `audible_goodreads_deal_scout/delivery.py`: config, cron, and delivery helpers
- `audible_goodreads_deal_scout/public_cli.py`: setup and CLI entrypoint
- `config.example.json`: public config example
- `examples/preferences.example.md`: sample notes file
- `tests/test_audible_goodreads_deal_scout.py`: skill-specific tests

## Publish to ClawHub

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

## Why this is worth publishing

The value is not just “show me today’s Audible deal.”

The real value is:
- filtering instead of deal spam
- combining public quality with personal fit
- respecting shelves like `read`, `currently-reading`, and `to-read`
- optional proactive delivery into a real channel
- graceful handling of suppressions and lookup failures
