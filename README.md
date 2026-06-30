# Claude Telegram Bridge

Control an already-running interactive Claude Code session from Telegram.

This bridge polls Telegram, pastes incoming messages into a visible tmux Claude
Code pane, tails Claude transcript JSONL, and sends only the matching final
answer back to Telegram.

The bridge is Claude-specific. It is not a general multi-AI bridge and it does
not share runtime code with the Codex Telegram Bridge.

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

## Files

- `claude_telegram_bridge.py` - bridge daemon.
- `hooks/claude-telegram-bridge-session-start.sh` - records transcript and tmux
  pane binding for the daemon.
- `hooks/claude-telegram-bridge-pretool-block.sh` - blocks Telegram MCP replies
  during bridge-owned turns.
- `config.example.env` - local environment template.

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

7. Register the SessionStart hook in Claude Code settings. The exact settings
file location depends on your Claude Code install.

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
- Unsafe slash commands from Telegram are escaped before entering Claude.
- The PreToolUse egress guard is included for users who also have Telegram MCP
  reply tools installed.
- Outgoing media auto-send is not part of this minimal export.

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

## Release Checklist

See `RELEASE_CHECKLIST.md` before publishing a fork or release.
