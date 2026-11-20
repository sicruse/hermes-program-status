# Changelog

All notable changes to this project are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## Unreleased

### Added

- `hermes --tui` sessions in herdr-core terminals (Linux): the agent half finds the TUI front end through `/proc`, and `on_session_open` reports `idle` when the TUI opens a session.
- A copy-and-paste installation prompt in the README, a profile-aware agent installation guide with approval checkpoints, and a next-steps note displayed by the Hermes installer.

## 0.1.0 - 2026-11-18

### Added

- First release. Reports `idle`, `working`, `blocked`, `done` and `error` for classic Hermes CLI sessions in herdr-core terminals, including when turns run in the Hermes gateway.
- Opt-in reporting in any terminal (`HERMES_PROGRAM_STATUS_TERMINALS=any`).
- Herdr TUI panes are skipped.
