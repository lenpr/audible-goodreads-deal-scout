# What Good ClawHub Skills Tend To Do Well

This repo was compared against a set of prominent ClawHub skills and public examples.

The repeated patterns were:

- README starts with user outcome, not implementation detail
- Quick start is short and numbered
- Privacy/data use is stated early
- Supported capabilities are described conservatively
- `SKILL.md` stays runtime-focused
- Public repos include polish files such as `LICENSE.txt` and `CHANGELOG.md`

The main adjustments made here to align with that:

- stronger top-of-README onboarding
- clearer privacy and delivery guidance
- supported marketplace list framed as tested support, not just config keys
- explicit release checklist
- `.clawhubignore`, `LICENSE.txt`, and `CHANGELOG.md`

Remaining improvement targets:

- continue splitting `core.py`
- keep real runtime smoke tests part of every release
- keep marketplace claims conservative if live pages drift
