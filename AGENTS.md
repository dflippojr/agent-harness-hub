# Agent notes

- To prove a change works in the real Hub console, use the verify skill: [.claude/skills/verify/SKILL.md](.claude/skills/verify/SKILL.md) (it builds on [docs/ui-testing.md](docs/ui-testing.md)).
- The fakes and the real daemon are held to one contract: [docs/daemon-contract.md](docs/daemon-contract.md). A change to how the Hub calls the daemon changes `tests/contract/endpoints.py`, a case, and the fake, together; run the `[daemon]` cases with `HUB_E2E_DAEMON_SRC=<agent-harness checkout> pytest tests/contract -m e2e`.
- The agent-harness pin (`ref:` in `.github/workflows/ci.yml`) may be at most **14 days** old while agent-harness main has moved past it. The nightly `drift.yml` run opens a "Bump agent-harness pin to <sha>" issue when main passes, a `bug` issue when it fails, and comments when the pin is older than that.
