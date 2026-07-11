# Claude Telegram Bridge

Control an already-running interactive Claude Code session from Telegram.

This bridge polls Telegram, pastes incoming messages into a visible tmux Claude
Code pane, tails Claude transcript JSONL, and sends only the matching final
answer back to Telegram.

The bridge is Claude-specific. It is not a general multi-AI bridge and it does
not share runtime code with the Codex Telegram Bridge.

## Quick Start (No Prior Bot Experience Needed)

In plain words: Claude Code runs on your computer, and this bridge lets you
talk to that session from the Telegram app on your phone. You text your own
private Telegram bot, the bridge types your message into the live Claude Code
terminal, and when Claude finishes answering, the final answer is sent back to
your phone.

Private-chat replies omit node prefixes and leading decorative emoji. Group
chats keep node emoji so senders remain clear; reply quoting works the same on
both surfaces.

### What you need

- A computer (Linux or macOS) where Claude Code is installed and logged in.
- `tmux` and Python 3 installed on that computer.
- The Telegram app on your phone, plus `curl` on the computer for one setup
  step below.

### Recommended - run the setup wizard

Install the package, then run the six-step wizard:

```bash
pipx install claude-telegram-bridge
claude-telegram-bridge setup
```

The wizard validates the hidden BotFather token, waits for `/start` to detect
your chat id, writes `token.json` and `token-registry.json`, installs the
SessionStart hook, backs up and merges `~/.claude/settings.json`, installs the
local service/watchdog, and sends one test message. It preserves unrelated
Claude settings and does not replace an existing hook chain.

Native Windows bridging is not supported: Windows Claude cannot execute the
`.sh` SessionStart hook and the daemon requires tmux. Use WSL. If a `--user`
install is not on PATH, run `py -m bridge_setup setup` (or `doctor`) instead.

Check or remove the installation later with:

```bash
claude-telegram-bridge doctor
claude-telegram-bridge uninstall
claude-telegram-bridge uninstall --purge
```

The numbered steps below remain as the manual fallback and explain each file
the wizard manages.

### Step 1 - Create your own bot with BotFather

1. In Telegram, search for `@BotFather` (the official bot with a blue check)
   and open a chat with it.
2. Send `/newbot`.
3. BotFather asks for a display name. Type anything, for example
   `My Claude Bridge`.
4. BotFather asks for a username. It must be unique and end in `bot`, for
   example `my_claude_bridge_bot`.
5. BotFather replies with an HTTP API token that looks like
   `1234567890:AbCd...` (digits, a colon, then a long letter string). Copy
   it. This token is a secret: paste it only into local files on your
   computer, never into a Telegram chat and never into a public repository.

### Step 2 - Find your chat id

The bridge only answers one Telegram chat: yours. To learn your chat id:

1. Open a chat with your new bot in Telegram and send it any message, for
   example `hello`. (Bots cannot message you first; this step is required.)
2. On your computer, run the following with your token substituted:

```bash
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
```

3. In the JSON response, find `"chat":{"id":123456789,...}`. That number is
   your chat id. If the response is empty, send the bot another message and
   run the command again.

### Step 3 - Install the bridge

```bash
pipx install claude-telegram-bridge
```

If `pipx` is not installed yet (`pipx: command not found`), install it once and
reopen your terminal so it lands on PATH:

```bash
python3 -m pip install --user pipx
python3 -m pipx ensurepath
```

or skip pipx entirely:

```bash
pip install --user claude-telegram-bridge
```

### Step 4 - Save the token and registry (one paste)

This writes the two small local files the bridge requires: the token file and
the token ownership registry. The `read -r -s` prompt keeps the token out of
your shell history.

```bash
mkdir -p "$HOME/.config/claude-telegram-bridge"
read -r -s -p "Paste your bot token, then press Enter: " CLB_BOT_TOKEN; echo
CLB_BOT_TOKEN="$CLB_BOT_TOKEN" python3 - <<'PY'
import hashlib, json, os, pathlib
token = os.environ["CLB_BOT_TOKEN"].strip()
root = pathlib.Path.home() / ".config/claude-telegram-bridge"
(root / "token.json").write_text(json.dumps({"token": token}))
(root / "token.json").chmod(0o600)
token_id = hashlib.sha256(token.encode()).hexdigest()[:16]
(root / "token-registry.json").write_text(json.dumps({
    "tokens": {
        "default": {
            "token_id": token_id,
            "mode": "polling",
            "owner": "claude-telegram-bridge",
            "expected_consumer": "claude",
            "allow_delete_webhook": False
        }
    }
}, indent=2))
print("wrote token.json and token-registry.json")
PY
```

### Step 5 - Start Claude Code in tmux and register the hook

Start (or keep) a Claude Code session inside tmux:

```bash
tmux -L default new -s claude
claude --dangerously-skip-permissions
```

About `--dangerously-skip-permissions`: this flag lets Claude Code run its
tools (file edits, shell commands) without stopping to ask you for per-action
approval. It is suggested here because an unattended remote session cannot
click an approval dialog — a pending permission prompt would leave your
Telegram turn hanging until you return to the keyboard. Understand the
tradeoff before using it: Claude can then act on your machine without asking.
If you prefer to keep the permission prompts, start plain `claude` instead;
the bridge still works, and Telegram turns simply wait whenever Claude is
blocked on an approval dialog that you must answer at the terminal.

Then download the SessionStart hook and register it, so the bridge can find
Claude's transcript and capture final answers:

```bash
curl -fsSL -o "$HOME/.config/claude-telegram-bridge/session-start.sh" \
  https://raw.githubusercontent.com/ssamssae/claude-telegram-bridge/main/hooks/claude-telegram-bridge-session-start.sh
chmod +x "$HOME/.config/claude-telegram-bridge/session-start.sh"
```

Register the hook in `~/.claude/settings.json` — the user-level Claude Code
settings file. Create the file with exactly this content if it does not exist
yet:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/home/YOU/.config/claude-telegram-bridge/session-start.sh"
          }
        ]
      }
    ]
  }
}
```

Replace `/home/YOU/...` with the absolute path of the file you downloaded
above (`echo "$HOME/.config/claude-telegram-bridge/session-start.sh"` prints
it; `~` is not expanded inside settings.json, so use the full path).

If `~/.claude/settings.json` already has content, merge instead of
overwriting: keep your existing keys and add the `"SessionStart"` entry inside
your existing `"hooks"` object (or add the whole `"hooks"` object alongside
your other top-level keys). Then run `/clear` in the Claude session (or start
a new one) so the hook fires at least once.

### Step 6 - First run

Only three environment variables are needed; everything else has safe
defaults:

```bash
export CLB_TOKEN_FILE="$HOME/.config/claude-telegram-bridge/token.json"
export CLB_TOKEN_REGISTRY="$HOME/.config/claude-telegram-bridge/token-registry.json"
export CLB_CHAT_ID="123456789"   # your chat id from Step 2
claude-telegram-bridge
```

### Step 7 - Say hello

1. Send `/ping` to your bot. The bridge itself answers immediately; this
   proves token and chat id are correct.
2. Send a real prompt, for example `What directory are you in?`. You should
   see the message appear inside the tmux Claude pane, and Claude's final
   answer arrive back in Telegram.

If `/ping` works but a real prompt gets no reply, the usual cause is the
SessionStart hook from Step 5 not being registered or the Claude session not
having been restarted after registering it. The bridge's terminal log says
what it is waiting for.

Done. For running from a clone, all environment variables, slash command
behavior, and safety notes, keep reading below.

## What It Supports

- Text prompts from Telegram into a live Claude Code tmux session.
- Telegram photos and image documents saved locally, then injected as
  `local_path` prompts for Claude to inspect.
- Telegram voice, audio, video, animation, and document uploads saved locally
  with caption and metadata preserved.
- Optional audio transcription through a local command configured with
  `CLB_AUDIO_TRANSCRIBE_CMD`.
- Transcript-based final-answer extraction instead of screen scraping.
- Optional flow mirror: live "work in progress" card that mirrors each tool-use
  step of the current turn to Telegram so you can follow long runs.
- Single Telegram chat allowlist and token ownership registry.
- Duplicate-egress guard hooks for users who also have Telegram MCP reply tools
  or terminal mirror hooks installed.

This is not MCP. It is a local bridge daemon for one visible Claude Code
session.

## Public Export Model

This public repository is maintained from a private operator source through a
sanitized export step. The export keeps the reusable Claude bridge behavior,
hook templates, config examples, and documentation, while stripping private chat
ids, token paths, hostnames, node labels, and local automation paths before
release.

Claude Telegram Bridge and Codex Telegram Bridge remain separate programs
because they drive different interactive CLIs and transcript formats. The shared
maintenance rule is the same: keep one internal source for each bridge, generate
the public copy through an export script, and never publish private wrappers or
operator-specific trigger paths.

## Billing And Terms

This project does not use `claude -p` and does not create hidden one-shot
Claude sessions. It drives an interactive Claude Code session that you already
started.

The subscription billing classification of a 24/7 daemon that automatically
injects prompts into an interactive Claude Code session is unverified. This
project does not claim that this usage is subscription-safe. You are responsible
for deciding whether to run it under your account, plan, and applicable terms.

## Verified Backend

- Claude Code interactive tmux session: experimental, locally verified by the
  maintainers.
- Other AI CLIs: not supported.

## How It Works

```text
Telegram user message/media
  -> getUpdates polling
  -> single chat id allowlist
  -> media download to local state directory when present
  -> paste prompt envelope into tmux Claude pane
  -> SessionStart sidecar binds tmux pane to Claude transcript JSONL
  -> transcript tail finds the final answer for the injected nonce
  -> Telegram sendMessage
```

The bridge uses one Bot API egress path. The included hooks prevent Claude's
Telegram MCP reply tool and terminal mirror hooks from sending duplicate
answers while the bridge owns a turn.

## Slash Commands

Slash commands sent from Telegram are classified before anything is injected
into the Claude pane. Commands that would open an interactive picker or dialog
are never pasted raw, because a blocking dialog can freeze the one visible
session. Each command falls into one of these groups.

| Command | Behavior |
| --- | --- |
| `/context` | Read-only context screen. The bridge widens the tmux capture window, runs `/context`, captures the pane with ANSI color, renders it locally to PNG, and replies with `sendPhoto` to the same Telegram chat. |
| `/usage`, `/cost` | Read-only info commands. The bridge widens the tmux capture window, waits for the render to finish, trims terminal chrome to a clean text view, and mirrors that text back to Telegram. |
| `/model` | Intercepted. Pasting `/model` raw opens an interactive picker that can freeze the session, so the bridge shows an inline keyboard of model choices instead and applies the pick non-interactively as `/model <alias>`. |
| `/clear`, `/exit`, `/quit` | Passed through unchanged; these do not open a dialog. `/exit` and `/quit` end the session, so the bridge triggers watchdog recovery for a graceful restart afterward. `/clear` only resets context. |
| `/ping`, `/start`, `/status` | Bridge health and status, answered by the bridge itself. |
| Anything else | Blocked from injection as a freeze-guard fail-safe. The bridge replies with the supported list instead of risking a stuck session. |

To bypass the freeze guard and inject a slash command raw, prefix the Telegram
message with `!` (for example `!/theme`).

Capture tuning for `/context`, `/usage`, and `/cost`:

- `CLB_CONTEXT_SETTLE_SEC` - floor delay before the first capture attempt.
  Defaults to `1.2` seconds.
- `CLB_CONTEXT_CAPTURE_TIMEOUT_SEC` - how long to keep polling for a finished
  render before sending the last captured frame. Defaults to `8.0` seconds.
- `CLB_CONTEXT_IMAGE_FONT` - optional local monospace font path used when
  rendering `/context` PNG output. Defaults to common system monospace fonts.
- `CLB_CONTEXT_IMAGE_FONT_SIZE` - `/context` PNG font size. Defaults to `17`.
- `CLB_MODEL_CHOICES` - optional comma-separated list of model aliases shown in
  the `/model` inline keyboard.

## Files

- `bridge_setup.py` - six-step setup wizard, doctor, and uninstall CLI.
- `bridge_watchdog.py` - local service recovery helper installed by the wizard.
- `claude_telegram_bridge.py` - bridge daemon.
- `hooks/claude-telegram-bridge-session-start.sh` - records transcript and tmux
  pane binding for the daemon.
- `hooks/claude-telegram-bridge-pretool-block.sh` - blocks Telegram MCP replies
  during bridge-owned turns.
- `config.example.env` - local environment template.

## Install

Install from PyPI with `pipx` (recommended) or `pip`:

```bash
pipx install claude-telegram-bridge
```

```bash
pip install --user claude-telegram-bridge
```

This provides the `claude-telegram-bridge` command and its `setup`, `doctor`,
and `uninstall` subcommands. With no subcommand it starts the daemon as before.
Installed copies also get
the startup version check with a one-tap Telegram update button. Running from
a clone works too: use `python3 claude_telegram_bridge.py` in place of the
installed command.

## Minimal Manual Setup

1. Start Claude Code in tmux:

```bash
tmux -L default new -s claude
claude --dangerously-skip-permissions
```

2. Create a Telegram bot with BotFather. Paste the bot token only into your
local terminal or local config file. Never send it in Telegram.

3. Copy the example config:

```bash
cp config.example.env .env
chmod 600 .env
```

4. Edit `.env` and set:

```bash
CLB_TOKEN_FILE="$HOME/.config/claude-telegram-bridge/token.json"
CLB_CHAT_ID="123456789"
CLB_TOKEN_REGISTRY="$HOME/.config/claude-telegram-bridge/token-registry.json"
CLB_STATE_DIR="$HOME/.local/state/claude-telegram-bridge"
```

5. Store the token locally:

```bash
mkdir -p "$HOME/.config/claude-telegram-bridge"
printf '{"token":"%s"}\n' 'PASTE_BOTFATHER_TOKEN_HERE' \
  > "$HOME/.config/claude-telegram-bridge/token.json"
chmod 600 "$HOME/.config/claude-telegram-bridge/token.json"
```

6. Create a token registry entry. The `token_id` is the first 16 hex chars of
the SHA-256 of the token.

```bash
python3 - <<'PY'
import hashlib, json, pathlib
root = pathlib.Path.home() / ".config/claude-telegram-bridge"
token = json.loads((root / "token.json").read_text())["token"]
token_id = hashlib.sha256(token.encode()).hexdigest()[:16]
(root / "token-registry.json").write_text(json.dumps({
    "tokens": {
        "default": {
            "token_id": token_id,
            "mode": "polling",
            "owner": "claude-telegram-bridge",
            "expected_consumer": "claude",
            "allow_delete_webhook": False
        }
    }
}, indent=2) + "\n")
PY
```

7. Register the SessionStart hook in Claude Code settings. The user-level
settings file is `~/.claude/settings.json` (project-level
`.claude/settings.json` inside a repository also works). Create it if missing;
if it already exists, merge the `hooks` block below into your existing JSON
instead of replacing the file.

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/absolute/path/to/hooks/claude-telegram-bridge-session-start.sh"
          }
        ]
      }
    ]
  }
}
```

8. Run the daemon:

```bash
set -a
. ./.env
set +a
python3 claude_telegram_bridge.py
```

Send `/ping` to the bot, then send a normal prompt.

## Safety Defaults

- Polling only; no public webhook is required.
- One allowed Telegram chat id.
- Token ownership registry must match the local token before polling starts.
- Interactive slash commands that could open a blocking dialog are held back by
  a freeze guard rather than pasted raw; a set of read-only and safe commands is
  supported and mirrored. See [Slash Commands](#slash-commands) for the full set
  and the `!` escape hatch.
- The PreToolUse egress guard is included for users who also have Telegram MCP
  reply tools installed.
- Outgoing media is limited to bridge-owned local outputs such as `/context`
  PNG replies; assistant answer attachment auto-discovery is not part of this
  minimal export.

## Optional Settings

- `CLB_FLOW_MIRROR_FLAG` - path to a flag file that enables the flow mirror.
  When the file exists, the bridge mirrors each tool-use step of the current
  turn to Telegram as one live-updating card. Off by default (no file). Enable
  with `touch "$HOME/.config/claude-telegram-bridge/flow-mirror.on"` and disable
  by removing the file.
- `CLB_ACTIVE_TURN_STALE_SECONDS` - releases a previously observed Telegram turn
  when Claude is idle but no final reply was captured, so later queued messages
  can continue instead of being blocked behind a stale active turn. Defaults to
  900 seconds.

### Advanced settings

The bridge reads ~20 more tuning knobs (self-update via `CLB_AUTO_UPDATE` /
`CLB_NO_UPDATE_CHECK`, send-retry policy, durable queue/outbox paths, session
binding TTLs, an optional `CLB_WATCHDOG_SCRIPT` lifecycle hook, and more). All
of them ship with safe defaults; the full annotated list lives at the bottom
of `config.example.env`.

## Release Checklist

See `RELEASE_CHECKLIST.md` before publishing a fork or release.
