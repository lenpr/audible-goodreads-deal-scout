# Release Checklist

Use this before publishing a new version to ClawHub.

## Code and tests

- Run `python3 -m py_compile audible_goodreads_deal_scout/*.py tests/*.py`
- Run `python3 -m unittest discover -s tests -p 'test_*.py'`
- Run `sh ./scripts/audible-goodreads-deal-scout.sh publish-audit --version <version> --tags latest`
- Run a short offline or cached Want-to-Read scan with `--progress json`, `--output-json`, and `--output-md` to verify progress and report output stay separate
- Keep the documented `sh ./scripts/...` form unless you have verified your target install preserves executable bits on bundled scripts
- Optional: run the local OpenClaw skill packager/validator against `.` if you use that publish workflow on this machine

## Runtime confidence

- Run at least one real OpenClaw runtime smoke for a positive recommendation
- Run at least one short-circuit case:
  - already read
  - no Goodreads match
  - Goodreads lookup failure
- Verify the fit paragraph still mentions:
  - what is likely to appeal
  - one credible downside
  - the `to-read` shelf when applicable

## Marketplace confidence

- Confirm all published marketplaces still have fixture coverage
- If a marketplace is flaky or structurally changed, downgrade or remove it before release

## Privacy and publishing

- Confirm `publish-audit` reports no private identifiers
- Confirm `.clawhubignore` excludes tests, docs, and local/generated state
- Confirm `TRUST.md` is current and included in the published bundle
- Confirm `SKILL.md` and `TRUST.md` clearly state that the skill reports opportunities only and does not buy, reserve, check out, redeem credits, manage subscriptions, or complete purchases
- Confirm placeholder paths and example content stay generic and public-safe
- Confirm `README.md`, `CHANGELOG.md`, and `config.example.json` still match the current behavior
- Keep public wording specific to evaluation and delivery; avoid language that implies checkout, payments, or wallet behavior if the skill does not actually do those things
- Confirm `audible-auth*.json`, cache files, reports, and generated `.audible-goodreads-deal-scout/` state are not included in the published bundle

## Versioning

- Bump version in your release command
- Add the release note to `CHANGELOG.md`
- Create a matching Git tag for the released commit, for example `git tag v0.1.11`
- Push the commit and matching tag to GitHub before or immediately after publishing, for example `git push origin main --tags`
- Create or update the GitHub release for the matching tag with the same release notes used on ClawHub
- Publish manually with `clawhub publish . ...` only after confirming the version and changelog text
- After publish, run `clawhub inspect <slug> --files` and confirm the bundled wrapper and license files match what `SKILL.md` and the repo root document
- After publish, run `clawhub inspect <slug>` and confirm the displayed marketplace license matches the intended license declared in `SKILL.md` and `LICENSE.txt`
