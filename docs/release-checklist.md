# Release Checklist

Use this before publishing a new version to ClawHub.

## Code and tests

- Run `python3 -m py_compile audible_goodreads_deal_scout/*.py tests/*.py`
- Run `python3 -m unittest discover -s tests -p 'test_*.py'`
- Run `python3 -m audible_goodreads_deal_scout.public_cli publish-audit --version <version> --tags latest`

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
- Confirm `.clawhubignore` excludes tests and local state
- Confirm `README.md`, `CHANGELOG.md`, and `config.example.json` still match the current behavior

## Versioning

- Bump version in your release command
- Add the release note to `CHANGELOG.md`
- Publish with `clawhub skill publish . ...`
