# Changelog

## 0.1.3

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
