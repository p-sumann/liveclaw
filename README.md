<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="voice/assets/livekit-logo.svg">
    <img src="voice/assets/livekit-logo-dark.svg" alt="LiveKit" height="36">
  </picture>
  &nbsp;&nbsp;<strong style="font-size: 20px">×</strong>&nbsp;&nbsp;
  <picture>
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/openclaw/openclaw/main/docs/assets/openclaw-logo-text-dark.png">
    <img src="https://raw.githubusercontent.com/openclaw/openclaw/main/docs/assets/openclaw-logo-text.png" alt="OpenClaw" height="36">
  </picture>
</p>

<h1 align="center">LiveClaw</h1>

<p align="center">
  <strong>Voice interface for <a href="https://github.com/openclaw/openclaw">OpenClaw</a>, powered by <a href="https://livekit.io">LiveKit</a></strong>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge" alt="MIT License"></a>
  <a href="https://livekit.io"><img src="https://img.shields.io/badge/Built_with-LiveKit-blueviolet?style=for-the-badge" alt="Built with LiveKit"></a>
  <a href="https://github.com/openclaw/openclaw"><img src="https://img.shields.io/badge/Gateway-OpenClaw-orange?style=for-the-badge" alt="OpenClaw Gateway"></a>
</p>

---

Talk to your OpenClaw AI assistant by voice — send messages on Telegram, research topics, check channel status, view chat history, all hands-free.

> **What's in this repo?** The root directory is an unmodified copy of the [OpenClaw](https://github.com/openclaw/openclaw) gateway. The `voice/` directory is the new addition — a LiveKit voice agent that connects to OpenClaw and exposes its messaging tools as voice commands. If you already run OpenClaw, you only need the `voice/` folder.

## Architecture

```
                        ┌─────────────────────────────────────┐
                        │           LiveKit Cloud             │
                        │                                     │
User (voice) ──────────►│  Room  ───► Voice Agent             │
                        │               │                     │
                        │          Deepgram STT               │
                        │          OpenAI LLM                 │
                        │          Deepgram TTS               │
                        └───────────────┬─────────────────────┘
                                        │ WebSocket
                                        ▼
                        ┌──────────────────────────────────────┐
                        │         OpenClaw Gateway             │
                        │                                      │
                        │   Telegram ─ WhatsApp ─ Discord      │
                        │   Slack ─ Signal ─ Google Chat       │
                        │   iMessage ─ Teams ─ Matrix          │
                        └──────────────────────────────────────┘
```

The **voice agent** (in `voice/`) runs on [LiveKit](https://livekit.io) with Deepgram STT/TTS and OpenAI LLM. It connects to an [OpenClaw](https://github.com/openclaw/openclaw) gateway via WebSocket and exposes messaging tools as voice commands.

## Prerequisites

| Requirement                                                                                           | Purpose                                               |
| ----------------------------------------------------------------------------------------------------- | ----------------------------------------------------- |
| **Python 3.12+**                                                                                      | Voice agent runtime                                   |
| **[uv](https://docs.astral.sh/uv/)**                                                                  | Python package manager                                |
| **Node.js 22+** and **[pnpm](https://pnpm.io)** (`npm i -g pnpm`)                                     | OpenClaw gateway _(skip if you already run OpenClaw)_ |
| **[LiveKit Cloud](https://cloud.livekit.io) account**                                                 | Real-time voice transport (free tier works)           |
| **[OpenAI API key](https://platform.openai.com/api-keys)**                                            | Voice agent LLM (GPT-4.1-mini), STT, TTS              |
| **[Deepgram API key](https://console.deepgram.com/)**                                                 | Speech-to-text and text-to-speech                     |
| **[Anthropic](https://console.anthropic.com/) or [OpenAI](https://platform.openai.com/api-keys) key** | Gateway agent LLM (for research via `ask_agent`)      |

## Getting Started

### If you already have OpenClaw running

Your gateway config and channels are already set up — you only need the `voice/` directory.

```bash
cd voice
uv sync
cp .env.example .env.local
```

Fill in your credentials in `.env.local` (see [Environment Variables](#environment-variables)), then:

```bash
uv run python agent.py dev
```

That's it — speak into your microphone to interact.

### Starting from scratch

You'll run two processes — the OpenClaw gateway and the voice agent — each in its own terminal.

#### 1. Clone and install

```bash
git clone https://github.com/p-sumann/liveclaw.git
cd liveclaw
pnpm install
```

#### 2. Run the onboarding wizard

```bash
pnpm dev onboard
```

This creates `~/.openclaw-dev/` and walks you through gateway setup — auth token, channels, agent identity, etc.

#### 3. Configure Telegram

If the wizard didn't set it up, create a bot via [@BotFather](https://t.me/BotFather) on Telegram and edit `~/.openclaw-dev/openclaw.json`:

```json
{
  "channels": {
    "telegram": {
      "dmPolicy": "open",
      "botToken": "<your-bot-token-from-botfather>",
      "allowFrom": ["*"]
    }
  }
}
```

> **Note:** When `dmPolicy` is `"open"`, you must include `"allowFrom": ["*"]` or the config validation will fail and Telegram won't load.

#### 4. Add the gateway LLM key

The gateway's own agent (the one that handles research via `ask_agent`) needs an LLM provider key:

```bash
echo 'ANTHROPIC_API_KEY=sk-ant-...' > ~/.openclaw-dev/.env
```

> **Important:** Without this, the voice agent will connect and basic commands (send message, check channels) will work — but research/`ask_agent` calls will fail because the gateway has no LLM configured. You can use `OPENAI_API_KEY` instead if you prefer.

#### 5. Start the gateway (Terminal 1)

```bash
pnpm dev --dev gateway
```

This auto-builds TypeScript (if `dist/` doesn't exist) and starts the gateway on port `19001`.

Note the **auth token** from `~/.openclaw-dev/openclaw.json` → `gateway.auth.token` — you'll need it for the voice agent.

#### 6. Set up the voice agent (Terminal 2)

```bash
cd voice
uv sync
cp .env.example .env.local
```

Edit `voice/.env.local` with your credentials — see [Environment Variables](#environment-variables) below.

| Variable                     | Value                                              |
| ---------------------------- | -------------------------------------------------- |
| `LIVEKIT_URL`                | `wss://your-project.livekit.cloud`                 |
| `LIVEKIT_API_KEY`            | From [LiveKit dashboard](https://cloud.livekit.io) |
| `LIVEKIT_API_SECRET`         | From LiveKit dashboard                             |
| `OPENAI_API_KEY`             | Your OpenAI key                                    |
| `DEEPGRAM_API_KEY`           | Your Deepgram key                                  |
| `OPENCLAW_GATEWAY_URL`       | `ws://127.0.0.1:19001`                             |
| `OPENCLAW_GATEWAY_TOKEN`     | Must match gateway config token                    |
| `DEFAULT_TELEGRAM_RECIPIENT` | Type `/whoami` to your bot                         |

#### 7. Run the voice agent

```bash
uv run python agent.py dev
```

Speak into your microphone — you're live.

## Environment Variables

### Voice agent (`voice/.env.local`)

| Variable                     | Required | Default                | Description                                             |
| ---------------------------- | -------- | ---------------------- | ------------------------------------------------------- |
| `LIVEKIT_URL`                | Yes      | —                      | LiveKit Cloud project URL                               |
| `LIVEKIT_API_KEY`            | Yes      | —                      | LiveKit API key                                         |
| `LIVEKIT_API_SECRET`         | Yes      | —                      | LiveKit API secret                                      |
| `OPENAI_API_KEY`             | Yes      | —                      | OpenAI API key for GPT-4.1-mini                         |
| `DEEPGRAM_API_KEY`           | Yes      | —                      | Deepgram API key for STT + TTS                          |
| `OPENCLAW_GATEWAY_URL`       | No       | `ws://127.0.0.1:18789` | Gateway WebSocket URL (`19001` for dev mode)            |
| `OPENCLAW_GATEWAY_TOKEN`     | Yes      | —                      | Must match `gateway.auth.token` in your OpenClaw config |
| `DEFAULT_TELEGRAM_RECIPIENT` | No       | —                      | Your numeric Telegram user ID (from `/whoami`)          |

### Gateway (`~/.openclaw-dev/.env` or `~/.openclaw/.env`)

The gateway needs its own environment variables. These go in the OpenClaw config directory, not in `voice/`:

| Variable            | Required | Description                                      |
| ------------------- | -------- | ------------------------------------------------ |
| `ANTHROPIC_API_KEY` | Yes\*    | LLM for the gateway agent (`ask_agent` research) |

> \* Either `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` — the gateway needs at least one LLM provider for research to work. The Telegram bot token goes in `openclaw.json` under `channels.telegram.botToken`.

## Voice Commands

Once running, try saying:

- **"Send a message to [name] on Telegram saying [message]"**
- **"Research the latest news about [topic]"** — streams the response as speech
- **"Send that to [name] on Telegram"** — sends the last research result without re-researching
- **"What channels are online?"**
- **"Show me recent chat sessions"**
- **"Check the gateway health"**

## Project Structure

```
liveclaw/
  src/                    # OpenClaw gateway source (unmodified)
  package.json            # Gateway dependencies
  extensions/             # OpenClaw plugins
  skills/                 # OpenClaw skills
  voice/                  # ← LiveKit voice agent (the new part)
    agent.py              # Voice agent with tools and entrypoint
    openclaw_client.py    # WebSocket client for OpenClaw gateway
    .env.example          # Environment variable template
    pyproject.toml        # Python project config
    assets/               # Branding assets (logos)
```

## Troubleshooting

### `unsupported channel: telegram`

The Telegram plugin is not loaded in the gateway. Common causes:

- **Missing `allowFrom`** — if `dmPolicy` is `"open"`, you must set `"allowFrom": ["*"]`
- **Missing bot token** — set `botToken` in `~/.openclaw-dev/openclaw.json` under `channels.telegram`
- **Wrong config file** — dev mode uses `~/.openclaw-dev/openclaw.json`

### `chat not found (chat_id=@username)`

Telegram Bot API requires a **numeric chat ID** for DMs, not a username. Use `/whoami` on your bot to get the numeric ID and set it in `DEFAULT_TELEGRAM_RECIPIENT`.

### `Timed out waiting for gateway handshake`

- Verify the gateway is running (check the terminal running `pnpm dev --dev gateway`)
- Check the port: default is `18789` (prod) or `19001` (dev mode)
- If using LAN, set `gateway.bind` to `"lan"` not `"loopback"`

### `Authentication failed`

Token mismatch between the voice agent and gateway:

- Check `OPENCLAW_GATEWAY_TOKEN` in `voice/.env.local`
- Compare with `~/.openclaw/openclaw.json` → `gateway.auth.token`

### `Connection refused`

- Start the gateway: `pnpm dev --dev gateway`
- Check logs: `tail -f ~/.openclaw-dev/logs/gateway.log` (dev) or `~/.openclaw/logs/gateway.log` (prod)

## License

[MIT License](LICENSE)
