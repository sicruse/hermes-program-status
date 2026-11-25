# hermes-program-status

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin that reports the live status of a classic `hermes` CLI session to its terminal with OSC 7501, the [Program Status Protocol](https://mitchellh.com/writing/program-status-osc7501).

The first terminal that uses those reports is herdr-core, the engine under the [Herdr](https://herdr.dev) desktop app and the Herdr phone app. With this plugin, a Hermes session in a Herdr terminal shows up as an agent with a status (idle, working, needs you, done) on the desktop and on the phone, the same way Claude Code and Codex do. Without it, Herdr shows Hermes as a plain shell.

## Why it exists

Herdr lists a terminal as an agent only when the program in it sends OSC 7501 reports. Hermes does not send them yet. The Hermes integration that Herdr installs (`herdr integration install hermes`) talks to the older Herdr TUI and never fires in herdr-core terminals ([herdr-app#358](https://github.com/herdrdev/herdr-app/issues/358)).

A plugin cannot simply write to its terminal either. When Hermes runs its gateway, the `hermes` CLI is a viewer: every turn, tool call, approval and question runs in the gateway process, which has no terminal. Plugin hooks fire there.

This plugin bridges that gap without patching Hermes. It uses only documented plugin hooks and the public `hermes_constants` module, passes `hermes plugins validate` (including the "no core override" check), and needs no change to the `hermes` launcher.

## What it reports

| When | Status sent |
|---|---|
| The CLI starts in a supported terminal | `idle` |
| A turn starts | `working` |
| Hermes asks you a question, or waits for an approval from you | `blocked` |
| You answer or decide | `working` |
| The turn finishes | `done` |
| The turn finishes while background subagents still run | `working`, until the turn that receives their results finishes |
| The run fails | `error` |
| You interrupt the run | `idle` |
| Hermes exits | cleared by the terminal when the shell returns |

Approvals decided by Hermes's "smart" approval model do not count as blocked, because nobody is waiting on you. Each report carries `app=hermes`, so Herdr names the agent.

## Requirements

- Hermes Agent 0.23.1 or newer, using the classic CLI (`hermes`). The Ink interface (`hermes --tui`) is not covered.
- macOS or Linux. Windows is not supported.
- A terminal that consumes OSC 7501. By default the plugin reports only inside herdr-core terminals (those started by the Herdr desktop app or `hc`). See Settings to report in other terminals.

Herdr TUI panes are left alone. Herdr TUI detects Hermes on its own, so the two can run side by side.

## Install

### Ask Hermes to install it

You can let Hermes handle the commands. Open Hermes on the computer where you run your sessions and paste this message:

```text
Help me install hermes-program-status so my Hermes sessions show their
status in the Herdr desktop and phone apps.

Read the installation guide first:
https://raw.githubusercontent.com/sicruse/hermes-program-status/main/INSTALL_FOR_AGENTS.md

Check that my setup is supported. Ask which profiles I want to use,
show me what you would change, and wait for my approval before making
changes. Keep my existing instructions and settings. Ask before disabling
another plugin, and do not restart any service.

Afterward, check that the plugin is enabled in each profile I chose.
Help me test a new Hermes session in Herdr, and tell me what passed
and what still needs checking. If you cannot read the guide, stop and
ask me for it rather than guessing.
```

Your agent will check compatibility and guide you through the test. It should leave other profiles alone. The [installation guide](INSTALL_FOR_AGENTS.md) lists the steps and approval checkpoints.

### Install from the command line

Hermes plugins are installed and enabled per profile. To target the default profile explicitly:

```sh
hermes -p default plugins install sicruse/hermes-program-status --enable
```

For each named profile whose sessions you want to see, run the same command with `-p`:

```sh
hermes -p work plugins install sicruse/hermes-program-status --enable
hermes -p research plugins install sicruse/hermes-program-status --enable
```

or in one loop, listing your own profile names:

```sh
for profile in work research; do
  hermes -p "$profile" plugins install sicruse/hermes-program-status --enable
done
```

Enabling asks a running Hermes gateway to load the plugin straight away. If the command reports that a restart is required, restart the gateway when no agent is mid-turn, because a gateway restart interrupts work in progress for every profile it serves.

Sessions that were already open keep the plugins they started with. Start a new `hermes` session to pick it up.

## Check that it works

1. Open a terminal in the Herdr desktop app and start `hermes` (or `hermes -p <profile>`).
2. Run `hc ls`. The terminal's agent should read `hermes` with status `idle`.
3. Send a prompt. The status moves to `working`, then `done`.
4. Ask Hermes to ask you a question with its clarify tool. The terminal shows as needing you on the desktop and in the Herdr phone app until you answer.

## How it works

The plugin has two halves in one file.

- Front end. When the plugin loads in the CLI process, which owns the terminal, it checks that the terminal consumes OSC 7501. If so, it writes a small registry file naming its tty and process id, and reports `idle`. The file is removed when the CLI exits.
- Agent half. The status hooks (`pre_llm_call`, `post_llm_call`, `pre_approval_request`, `post_approval_response`, `on_human_input_request`, `on_human_input_resolved`, `on_session_end`) usually fire in the gateway. For each event the plugin takes the event's session id and finds the terminal showing that session. Hermes already records this in a per-terminal file, `<HERMES_HOME>/terminal-sessions/tty-<device>`, which the CLI keeps current for `hermes --continue`. The plugin matches that file against the registry and writes one OSC 7501 sequence to the tty device.

When the agent runs in the same process as the terminal (no gateway), the plugin writes to its own registered tty directly.

Sessions from messaging platforms, cron jobs and unregistered terminals never match, so the plugin does nothing for them.

## What it reads and writes

- Reads `<HERMES_HOME>/terminal-sessions/tty-*` for the default home and each profile home.
- Writes one small JSON file per live CLI session to `<Hermes root>/plugin-data/hermes-program-status/` (normally `~/.hermes/plugin-data/hermes-program-status/`, shared across profiles), and removes it on exit.
- Writes OSC 7501 sequences, about 30 bytes each, to tty devices that a live Hermes CLI of the same user registered.
- No network access, no subprocesses, no credentials, no telemetry. Failures are silent and never affect the agent.

## Settings

All optional, read from the environment of the Hermes process.

| Variable | Default | Effect |
|---|---|---|
| `HERMES_PROGRAM_STATUS_TERMINALS` | `herdr` | `herdr` reports only in herdr-core terminals. `any` reports in every terminal; use it only with terminals that ignore unknown OSC sequences. |
| `HERMES_PROGRAM_STATUS_APP` | `hermes` | App name sent with each report. Empty sends none. |
| `HERMES_PROGRAM_STATUS_LOG` | unset | Path of a JSON-lines trace of every report, for debugging. |
| `HERMES_PROGRAM_STATUS_REGISTRY` | `<Hermes root>/plugin-data/hermes-program-status` | Shared registry directory (normally under `~/.hermes`, not the active profile home). |
| `HERMES_PROGRAM_STATUS_HOMES` | default home plus every profile home | Hermes homes to search for terminal-session files, separated by `:`. |

## Limitations

- `done` stays until your next prompt, because Hermes has no hook for "the user came back".
- Right after a session changes (`/new`, `/resume`, or compression starting a new session id) there can be a short gap before the CLI rewrites its terminal-session file. Reports resume after that.
- The plugin depends on the `terminal-sessions` file name and content, which Hermes uses internally for `hermes --continue`. If a Hermes update changes that format, status stops until the plugin is updated. It fails quietly, not loudly.
- If `session.terminal_continue` is turned off in Hermes's config, there are no terminal-session files and the agent half cannot find the terminal when turns run in the gateway.

## Uninstall

For each profile where you installed it:

```sh
hermes plugins disable hermes-program-status
hermes plugins remove hermes-program-status
```

(add `-p <profile>` for named profiles).

## Toward a native fix

The cleanest long-term answer is for Hermes to send OSC 7501 itself from the CLI, which already receives every turn, approval and question event from the gateway. This plugin is meant as a working stopgap and a reference for that change. Once Hermes reports status natively, uninstall it.

## Contributing

Bug reports and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). Report security problems privately as described in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE). Copyright (c) 2026 Si Cruse Consulting Ltd.
