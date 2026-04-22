# Audible Goodreads Deal Scout

`audible-goodreads-deal-scout` is a **ClawHub / OpenClaw skill** for evaluating Audible daily deals.

If you are looking at this repository on GitHub, you are looking at the **source for a publishable ClawHub skill**, not a generic Python app or a standalone website.

Its job is simple: help you decide whether an Audible daily promotion is actually worth your attention.

It is built for people who do **not** want raw deal spam. Instead of only showing the discounted book, the skill combines:
- the public Goodreads rating
- your Goodreads shelves, if you provide a CSV
- your own reading preferences, if you provide notes
- a delivery policy that decides what should be sent and what should be skipped quietly

## What this skill is for

Use this skill if you want OpenClaw to:
- check the current Audible daily promotion
- decide whether the book clears a quality bar
- notice if you already read it or already saved it
- write a short fit paragraph about why it may work for you
- optionally send the result to a configured channel such as Telegram

This repo is intended to be:
- developed on GitHub
- published on ClawHub
- installed into OpenClaw as a reusable skill

If you want to re-implement the workflow without using the shipped skill directly, see [PROMPT_REQUEST.md](PROMPT_REQUEST.md). It captures the intention, scope, design guardrails, edge cases, and example prompt in one place.

## Install or publish

If you just want to use it in OpenClaw once it is published:

```bash
openclaw skills install audible-goodreads-deal-scout
```

Start a new OpenClaw session after install or after changing skill config so the fresh skill snapshot is picked up cleanly.

If you want to publish your own version from this repo:

```bash
clawhub login
clawhub publish . \
  --slug audible-goodreads-deal-scout \
  --name "Audible Goodreads Deal Scout" \
  --version 0.1.2 \
  --changelog "Rename published wrapper and license files for ClawHub packaging" \
  --tags latest
```

## Start here

Use this skill if you want:
- a daily Audible deal filter instead of a daily Audible deal feed
- a way to suppress books you already read
- a way to fast-track books you already saved on Goodreads
- a short fit paragraph that explains why a book may work for you, and what may not

You get the most value from it if you provide both:
- a Goodreads library export CSV
- a short notes file about your taste

You can still use it without both:
- no CSV, no notes: public Goodreads signal only
- notes only: public Goodreads signal plus a notes-driven fit paragraph
- CSV only: shelf logic plus fit from your Goodreads history

## 5-minute setup

If you want one straightforward setup path, use this:

1. Pick your Audible store. If you do nothing, it defaults to `us`.
2. Export your Goodreads library CSV if you want personalization.
3. Optionally create a short notes file with what you like and dislike.
4. Run:

```bash
./scripts/audible-goodreads-deal-scout.sh setup
```

5. Then evaluate the current deal:

```bash
./scripts/audible-goodreads-deal-scout.sh prepare \
  --config-path .audible-goodreads-deal-scout/config.json
```

## How to get your Goodreads CSV

If you use Goodreads, the skill gets much stronger when you export your library.

The usual path is:
1. Open `My Books`
2. Open `Import and Export`
3. Choose `Export Library`
4. Wait for Goodreads to generate the file
5. Download the CSV and point the skill at it with `goodreadsCsvPath`

Why the CSV matters:
- it tells the skill what you already marked as `read`
- it tells the skill what is already on your `to-read` shelf
- it gives the fit step access to your ratings and written reviews

If your export is old, the skill will warn you, but it can still run.

## If you do not use Goodreads

That is still a valid setup.

You can use a notes-only workflow:
- no Goodreads CSV
- one strong text file about your taste

The better the notes, the better the fit paragraph.

Good notes usually include:
- 5 to 15 books or authors you genuinely liked
- a few examples of books you disliked and why
- genres you seek out
- genres you avoid
- pacing preferences
- tone preferences
- what feels too sentimental, too slow, too commercial, too dark, too clever, too flat, and so on

A strong notes file sounds like you talking to a smart bookseller, not like metadata.

Useful:

```md
I like morally serious fiction, political tension, and books with ideas.
I often like Orwell, Ishiguro, Le Guin, Vonnegut, and literary sci-fi.
I lose interest when a book gets too sentimental or too plot-mechanical.
I like books that are sharp, unsparing, and a bit strange, but still readable.
```

Too weak:

```md
I like good books.
```

If you do not have Goodreads, the best setup is:
- a thoughtful notes file
- the default public Goodreads check
- `deliveryPolicy: positive_only`

## Which mode should I use?

| Mode | What you provide | What you get |
| --- | --- | --- |
| `public` | nothing personal | Audible + Goodreads public score only |
| `notes` | a notes file or pasted text | Goodreads public score plus notes-driven fit |
| `full` | Goodreads CSV, optionally notes | shelf-aware suppression/recommendation plus richer fit |

## How the recommendation works

At a high level:
- the skill fetches the current Audible daily promotion
- it resolves the matching Goodreads page and public rating
- it applies your Goodreads shelf rules if you provided a CSV
- it uses your notes and/or Goodreads history to shape the fit paragraph
- it decides whether to deliver the result based on your delivery policy

### The default Goodreads threshold

The default threshold is `3.8`.

That means:
- a public Goodreads average rating of `3.80` or lower is treated as below your quality cutoff
- a rating above `3.8` is eligible

This is the **public Goodreads average**, not your own rating.

Good starting points:
- `4.0` if you want to be stricter
- `3.8` if you want a balanced default
- `3.6` or `3.7` if you want more deals to pass through

### Goodreads shelf rules

If you provide a Goodreads CSV:
- `read` => suppress
- `currently-reading` => suppress
- `to-read` => recommend immediately

That `to-read` override is intentional. If you already saved the book for later, that is treated as a strong positive signal and it can override the public Goodreads threshold.

## Supported marketplaces

Supported and fixture-tested marketplace keys:
- `us`
- `uk`
- `de`
- `ca`
- `au`

If you do nothing, the default is `us`.

Support here means the repo has fixture-backed coverage for:
- daily-promotion detection
- discounted price extraction
- book identity extraction

Live marketplace behavior can still vary. A supported store may still return:
- no active promotion
- a blocked page
- a page-layout drift error

If you want a non-US store, set `audibleMarketplace` to one of the keys above:

```json
{
  "audibleMarketplace": "uk"
}
```

## Privacy and data use

This part should be explicit.

- The Python prep layer reads your Goodreads CSV locally.
- The Python prep layer also reads your notes file locally.
- The model step may use fit-context data unless you set `privacyMode` to `minimal`.
- In `privacyMode: "minimal"`, the skill still uses local shelf logic, but it does **not** pass your personal CSV or notes content into the model fit step.
- Delivery targets are whatever you configure in your own OpenClaw runtime.

If you want the safest default for personal data sharing, use:

```json
{
  "privacyMode": "minimal"
}
```

## What a notes file should look like

It does **not** need strict structure. A short, honest, messy note is fine.

Example file: `examples/preferences.example.md`

Good notes usually include:
- books or authors you reliably like
- genres or tones you usually avoid
- pacing preferences
- whether you like literary, commercial, cerebral, emotional, satirical, dark, warm, and so on

Weak notes are still accepted, but they produce weaker fit output. For example, `I like good books` is valid input, but not very useful evidence.

## Delivery policies

`deliveryPolicy` controls how noisy the skill is:

| Policy | Best for | Behavior |
| --- | --- | --- |
| `positive_only` | most people | send only likely fits |
| `summary_on_non_match` | people who want visibility into skips | send full recommendations, but short summary cards for suppressions/errors |
| `always_full` | logging and audits | send every final result in full |

Recommended default:
- `positive_only`

That gives the best signal-to-noise ratio for most users.

## Telegram, WhatsApp, and other channels

This repository does **not** ship its own Telegram or WhatsApp connector.

Instead, it uses the OpenClaw message surface:
- `openclaw message send --channel ... --target ...`

That means delivery works whenever your OpenClaw environment already has a supported channel configured.

### Telegram

If your OpenClaw setup supports Telegram delivery, configure:

```json
{
  "deliveryChannel": "telegram",
  "deliveryTarget": "-1000000000000"
}
```

`deliveryTarget` is the Telegram chat or channel id you want to post into.

### WhatsApp

WhatsApp can work **only if** your OpenClaw install already exposes a WhatsApp-capable message channel.

In that case the pattern is the same:

```json
{
  "deliveryChannel": "whatsapp",
  "deliveryTarget": "<your-whatsapp-target>"
}
```

This repo does not create that WhatsApp channel. It only uses it if your OpenClaw runtime already provides it.

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

## One complete walkthrough

If you want to see the whole flow once from start to finish, this is the cleanest path.

1. Write a config and optional notes/delivery settings:

```bash
./scripts/audible-goodreads-deal-scout.sh setup \
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

2. Prepare today's deal:

```bash
./scripts/audible-goodreads-deal-scout.sh prepare \
  --config-path .audible-goodreads-deal-scout/config.json
```

That writes the working artifacts into `.audible-goodreads-deal-scout/artifacts/current/`, including:
- `prepare-result.json`
- `runtime-input.json`
- `runtime-prompt.md`
- `runtime-output-schema.json`

3. Let the OpenClaw runtime produce a `runtime-output.json` file that matches the schema from step 2. A typical successful output looks like:

```json
{
  "schemaVersion": 1,
  "goodreads": {
    "status": "resolved",
    "url": "https://www.goodreads.com/book/show/1",
    "title": "Signal Fire",
    "author": "Jane Story",
    "averageRating": 4.15,
    "ratingsCount": 9501
  },
  "fit": {
    "status": "written",
    "sentence": "Fit: Likely to work if you want sharp, idea-driven speculative fiction. The main risk is that it may feel more cerebral than emotionally warm."
  }
}
```

4. Finalize the public result contract:

```bash
./scripts/audible-goodreads-deal-scout.sh finalize \
  --prepare-json .audible-goodreads-deal-scout/artifacts/current/prepare-result.json \
  --runtime-output /tmp/runtime-output.json
```

5. If you configured delivery, send the finished message:

```bash
./scripts/audible-goodreads-deal-scout.sh run-and-deliver \
  --config-path .audible-goodreads-deal-scout/config.json \
  --prepare-json .audible-goodreads-deal-scout/artifacts/current/prepare-result.json \
  --runtime-output /tmp/runtime-output.json
```

If you just want to inspect the decision locally, stop after `finalize`.

## Troubleshooting

Three issues cause most confusing runs:

- Wrong notes path: if `notesFile` or `preferencesPath` points at a missing file, the prep step now returns `error_missing_notes_file` instead of silently continuing.
- Wrong CSV header override: `--csv-column role=Header` must match the Goodreads export header exactly. If you are unsure, run `show-csv-headers` first.
- Stale Goodreads export: if the CSV is old, the skill can still run, but read status, shelf state, and fit evidence may lag behind your actual library.

Useful checks:

```bash
./scripts/audible-goodreads-deal-scout.sh show-csv-headers "/absolute/path/to/goodreads_library_export.csv"
./scripts/audible-goodreads-deal-scout.sh publish-audit --version 0.1.2 --tags latest
```

If your OpenClaw host runs `exec` in `allowlist` mode, prefer this bundled wrapper instead of `python3 -m ...`. It gives the skill one narrow executable path that can be allowlisted cleanly for scheduled runs.
- Scheduled runs cannot stop for interactive exec approval. If your OpenClaw host keeps `exec` in `allowlist` mode, allowlist the installed wrapper path before enabling daily automation, for example `~/.openclaw/skills/audible-goodreads-deal-scout/scripts/audible-goodreads-deal-scout.sh`.

## Advanced CLI usage

If you prefer scripted setup instead of interactive setup:

```bash
./scripts/audible-goodreads-deal-scout.sh setup \
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

Useful helper commands:

```bash
./scripts/audible-goodreads-deal-scout.sh show-csv-headers "/absolute/path/to/goodreads_library_export.csv"
./scripts/audible-goodreads-deal-scout.sh measure-context --goodreads-csv "/absolute/path/to/goodreads_library_export.csv" --output /tmp/fit-context.json
./scripts/audible-goodreads-deal-scout.sh publish-audit --version 0.1.2
```

Finalize and deliver in one step:

```bash
./scripts/audible-goodreads-deal-scout.sh run-and-deliver \
  --config-path .audible-goodreads-deal-scout/config.json \
  --prepare-json .audible-goodreads-deal-scout/artifacts/current/prepare-result.json \
  --runtime-output /tmp/runtime-output.json
```

## Repository structure

- `SKILL.md`: agent-facing runtime instructions
- `agents/openai.yaml`: interface metadata and default prompt for OpenClaw agent surfaces
- `scripts/audible-goodreads-deal-scout.sh`: bundled wrapper entrypoint for local CLI and OpenClaw exec-friendly invocation
- `audible_goodreads_deal_scout/core.py`: prep/orchestration logic
- `audible_goodreads_deal_scout/rendering.py`: card rendering and delivery planning
- `audible_goodreads_deal_scout/delivery.py`: config, cron, and delivery helpers
- `audible_goodreads_deal_scout/public_cli.py`: setup and CLI entrypoint
- `config.example.json`: public config example
- `examples/preferences.example.md`: sample notes file
- `docs/release-checklist.md`: release checklist for publishable builds

## Publish to ClawHub

Before publishing, run:

```bash
./scripts/audible-goodreads-deal-scout.sh publish-audit --version 0.1.2 --tags latest
```

## Why this is worth publishing

The value is not just “show me today’s Audible deal.”

The real value is:
- filtering instead of deal spam
- combining public quality with personal fit
- respecting `read`, `currently-reading`, and `to-read`
- optional proactive delivery into a real channel
- graceful handling of suppressions and lookup failures
