"""LiveClaw Voice Agent — LiveKit voice interface for OpenClaw.

Provides a voice-controlled interface to OpenClaw's capabilities:
- Send messages via Telegram, WhatsApp, Discord, Slack, etc.
- Ask the OpenClaw agent to perform tasks (with streaming TTS)
- Check channel statuses and chat history

Features:
- Voice filler phrases before tool calls for natural UX
- Streaming TTS: agent responses are spoken as they arrive
- Auto-caching of research results for quick send via Telegram
"""

import asyncio
import json
import logging
import os
import random
import re
from dataclasses import dataclass, field
from typing import AsyncIterable

from dotenv import load_dotenv

from livekit import agents
from livekit.agents import (
    Agent,
    AgentSession,
    AgentServer,
    ConversationItemAddedEvent,
    RunContext,
    ToolError,
    function_tool,
    room_io,
)
from livekit.agents.llm import ChatMessage
from livekit.plugins import deepgram, silero

from openclaw_client import OpenClawClient

load_dotenv(".env.local")
logger = logging.getLogger("liveclaw")

# Default Telegram recipient for "send me" commands.
# Set via env var or leave empty to have the agent ask.
DEFAULT_TELEGRAM_RECIPIENT = os.environ.get("DEFAULT_TELEGRAM_RECIPIENT", "")

# Filler phrases spoken before tool calls for natural UX
THINKING_FILLERS = [
    "Sure, let me look into that.",
    "On it, give me a moment.",
    "Let me check that for you.",
    "Sure thing, working on it.",
    "Alright, let me find out.",
]

SENDING_FILLERS = [
    "Sending that now.",
    "Got it, sending the message.",
    "Alright, sending it over.",
]


@dataclass
class AppData:
    """Shared state passed to all tools via RunContext."""

    openclaw: OpenClawClient = field(default_factory=lambda: OpenClawClient("", ""))
    session: AgentSession | None = None
    # Store the last research result for send_last_research
    last_research: str = ""


async def _say_filler(session: AgentSession | None, fillers: list[str]) -> None:
    """Speak a random filler phrase for natural UX."""
    if session is None:
        return
    filler = random.choice(fillers)
    session.say(filler, allow_interruptions=True)
    # Don't await full playout — let it overlap with the tool work
    await asyncio.sleep(0.3)


def _sanitize_for_speech(text: str) -> str:
    """Strip markdown and convert numbers/prices to natural spoken form for TTS."""
    # Remove bold/italic markers: **text** → text, *text* → text, __text__ → text
    text = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", text)
    # Remove strikethrough: ~~text~~ → text
    text = re.sub(r"~~([^~]+)~~", r"\1", text)
    # Remove inline code: `code` → code
    text = re.sub(r"`([^`]+)`", r"\1", text)
    # Remove markdown headers: ### Header → Header
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Remove markdown links: [text](url) → text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Remove markdown images: ![alt](url) → alt
    text = re.sub(r"!\[([^\]]*)\]\([^)]+\)", r"\1", text)
    # Remove bullet markers at start of lines: - item or * item → item
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    # Remove numbered list markers: 1. item → item
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)

    # Convert prices: $0.03 → 3 cents, $1.50 → 1 dollar 50 cents, $25 → 25 dollars
    def _price_to_words(m: re.Match) -> str:
        amount = float(m.group(1))
        if amount < 0.01:
            return f"{amount:.4f} dollars"
        if amount < 1.0:
            cents = round(amount * 100)
            return f"{cents} cent{'s' if cents != 1 else ''}"
        dollars = int(amount)
        cents = round((amount - dollars) * 100)
        parts = [f"{dollars} dollar{'s' if dollars != 1 else ''}"]
        if cents > 0:
            parts.append(f"{cents} cent{'s' if cents != 1 else ''}")
        return " ".join(parts)

    text = re.sub(r"\$(\d+(?:\.\d+)?)", _price_to_words, text)

    # Convert percentages with decimals: 0.03% → 0.03 percent
    text = re.sub(r"(\d+(?:\.\d+)?)%", r"\1 percent", text)

    return text


async def _resolve_telegram_chat_id(client: OpenClawClient) -> str | None:
    """Resolve the user's numeric Telegram chat ID from gateway sessions.

    When a user presses /start on the bot, the gateway stores a session
    with key format 'agentId:telegram:<numericChatId>'. We scan sessions
    to find the Telegram DM session and extract the numeric ID.
    """
    try:
        result = await client.list_sessions()
        sessions = result.get("sessions", []) if isinstance(result, dict) else result
        if not isinstance(sessions, list):
            return None
        for s in sessions:
            key = s.get("key", "")
            channel = s.get("channel", "") or s.get("lastChannel", "")
            kind = s.get("kind", "")
            # Look for direct Telegram sessions
            if channel == "telegram" and kind == "direct":
                # Session key format: agentId:telegram:<numericChatId>
                # Extract the numeric chat ID from the key
                parts = key.split(":")
                for part in parts:
                    if part.lstrip("-").isdigit() and len(part) > 3:
                        return part
            # Also check lastTo field which may contain the numeric ID
            last_to = s.get("lastTo", "")
            if last_to and last_to.lstrip("-").isdigit() and channel == "telegram":
                return last_to
    except Exception as e:
        logger.warning("Failed to resolve Telegram chat ID from sessions: %s", e)
    return None


class OpenClawVoiceAgent(Agent):
    """Voice agent that controls OpenClaw."""

    def __init__(self) -> None:
        super().__init__(
            instructions=(
                "You are a voice assistant powered by OpenClaw X Livekit — a personal AI gateway "
                "that connects to messaging platforms like Telegram, WhatsApp, Discord, "
                "Slack, and more.\n\n"
                "You can help the user:\n"
                "- Send messages via Telegram (the primary connected channel)\n"
                "- Ask the OpenClaw agent to do tasks (research, write, plan, etc.)\n"
                "- Check which channels are online and their status\n"
                "- View recent chat history and sessions\n\n"
                "Keep responses concise and natural for voice. "
                "NEVER use markdown formatting — no asterisks, no bold, no headers, no bullet markers. "
                "Write everything as plain spoken English. "
                "For prices, say 'three cents' not '$0.03'. For percentages, say 'five percent' not '5%'. "
                "Speak numbers naturally — say 'one hundred and fifty' not '150'.\n\n"
                "MESSAGING: The default and primary channel is Telegram. When the user says "
                "'send me', 'message me', or 'send a summary', use send_message with "
                "channel='telegram'."
                + (
                    f" The user's default Telegram recipient is '{DEFAULT_TELEGRAM_RECIPIENT}'. "
                    "Always use this as the recipient when the user says 'send me' or 'message me'.\n\n"
                    if DEFAULT_TELEGRAM_RECIPIENT
                    else " Ask the user for their Telegram username or chat ID before sending.\n\n"
                ) +
                "IMPORTANT: Email is NOT supported. If asked, suggest Telegram instead.\n\n"
                "IMPORTANT: When using the ask_agent tool, the agent's response will be "
                "streamed directly as speech to the user. After calling ask_agent, "
                "DO NOT repeat or summarize what the agent said — just confirm the task "
                "was completed or ask if they need anything else.\n\n"
                "CACHING: After ask_agent runs, the result is automatically saved. "
                "When the user says 'send it', 'send that to my boss', 'message me that', "
                "or anything referring to a previous result — use send_last_research to send "
                "the cached result. NEVER call ask_agent again for the same topic. "
                "Only call ask_agent if the user asks about something NEW.\n\n"
                "WORKFLOW: If the user asks to research something and then send it:\n"
                "1. First use ask_agent to research the topic (result is auto-cached)\n"
                "2. Use send_last_research to send the cached result on Telegram"
            ),
        )

    async def on_enter(self) -> None:
        await self.session.generate_reply(
            instructions=(
                "Greet the user briefly. Tell them you're their LiveClaw voice assistant "
                "and you can help them send messages on Telegram, research topics, "
                "and check channel status. Keep it to one or two sentences."
            )
        )

    # ── Messaging tools ─────────────────────────────────────────────────

    @function_tool()
    async def send_message(
        self,
        context: RunContext[AppData],
        recipient: str,
        message: str,
        channel: str,
    ) -> str:
        """Send a message to someone on a messaging platform.
        Telegram is the primary channel. Email is NOT supported.

        Args:
            recipient: The recipient identifier — a Telegram user ID, username, phone number, or chat ID.
            message: The message text to send.
            channel: The messaging channel. Default to 'telegram'. Others: whatsapp, discord, slack, signal.
        """
        if channel.lower() == "email":
            raise ToolError(
                "Email is not supported. Use a chat channel like telegram, "
                "whatsapp, discord, slack, or signal instead."
            )

        await _say_filler(context.userdata.session, SENDING_FILLERS)
        client = context.userdata.openclaw
        try:
            result = await client.send_message(
                to=recipient, message=message, channel=channel
            )
            return f"Message sent successfully on {channel} to {recipient}."
        except Exception as e:
            raise ToolError(f"Failed to send message: {e}")

    @function_tool()
    async def send_last_research(
        self,
        context: RunContext[AppData],
        recipient: str,
        channel: str = "telegram",
    ) -> str:
        """Send the previously researched result that was already spoken to the user.
        Use this when the user says 'send it', 'send that', 'message me that',
        or 'send it to my boss' after a research task was already completed.
        This avoids re-researching — it sends the cached result directly.

        Args:
            recipient: The recipient identifier — a Telegram user ID or chat ID.
            channel: The messaging channel. Default to 'telegram'.
        """
        research = context.userdata.last_research
        if not research:
            raise ToolError(
                "No previous research to send. Use ask_agent first to research a topic."
            )

        await _say_filler(context.userdata.session, SENDING_FILLERS)
        client = context.userdata.openclaw
        try:
            result = await client.send_message(
                to=recipient, message=research, channel=channel
            )
            return f"Research summary sent to {recipient} on {channel}."
        except Exception as e:
            raise ToolError(f"Failed to send research: {e}")

    @function_tool()
    async def ask_agent(
        self,
        context: RunContext[AppData],
        task: str,
    ) -> str:
        """Ask the OpenClaw agent to perform a NEW task. Use this for research,
        writing, planning, code generation, web search, or any complex task
        that needs the full agent capabilities. The response will be spoken
        to the user directly via streaming TTS.

        The result is automatically cached — if the user later says 'send it'
        or 'send that to someone', use send_last_research instead of calling
        this tool again. Only call this for genuinely NEW tasks.

        Args:
            task: A description of what you want the agent to do.
        """
        await _say_filler(context.userdata.session, THINKING_FILLERS)

        client = context.userdata.openclaw
        session = context.userdata.session

        try:
            # Prepend instruction to keep response concise for voice
            voice_task = (
                "IMPORTANT: This response will be read aloud via text-to-speech. "
                "Keep it concise — 2-3 short paragraphs max. "
                "NEVER use markdown formatting (no asterisks, no bold, no headers, no bullet markers). "
                "Write all prices and numbers in spoken form (e.g. 'three cents' not '$0.03', "
                "'one hundred fifty' not '150'). Write in plain conversational English.\n\n"
                + task
            )

            # Use streaming to pipe response directly to TTS
            agent_stream = await client.agent_message_streaming(message=voice_task)

            # Stream the agent's response directly to TTS as it arrives
            if session is not None:
                collected_text: list[str] = []

                async def _text_generator() -> AsyncIterable[str]:
                    """Yield text deltas from the agent stream, sanitized for speech."""
                    async for delta in agent_stream:
                        cleaned = _sanitize_for_speech(delta)
                        collected_text.append(cleaned)
                        yield cleaned

                await session.say(
                    _text_generator(),
                    allow_interruptions=True,
                )

                # Store the research result for send_last_research
                full_text = "".join(collected_text)
                context.userdata.last_research = full_text

                return (
                    "Agent response has been spoken to the user. "
                    "The research is saved — use send_last_research to send it. "
                    "Do NOT repeat what the agent said."
                )
            else:
                # Fallback: collect full text if session not available
                full_text = ""
                async for delta in agent_stream:
                    full_text += delta
                context.userdata.last_research = full_text
                return full_text or "Agent task completed but produced no output."

        except Exception as e:
            raise ToolError(f"Failed to submit agent task: {e}")

    # ── Channel & status tools ──────────────────────────────────────────

    @function_tool()
    async def check_channels(
        self,
        context: RunContext[AppData],
    ) -> str:
        """Check which messaging channels are currently connected and their status.
        Use this when the user asks what channels are available or if a specific
        channel is online.
        """
        await _say_filler(context.userdata.session, THINKING_FILLERS)
        client = context.userdata.openclaw
        try:
            result = await client.get_channels_status()
            if isinstance(result, dict):
                channels = result.get("channels", result)
                summary_parts: list[str] = []
                if isinstance(channels, list):
                    for ch in channels:
                        name = ch.get("channel", ch.get("name", "unknown"))
                        status = ch.get("status", "unknown")
                        summary_parts.append(f"{name}: {status}")
                elif isinstance(channels, dict):
                    for name, info in channels.items():
                        status = info.get("status", "unknown") if isinstance(info, dict) else str(info)
                        summary_parts.append(f"{name}: {status}")
                return "Connected channels: " + ", ".join(summary_parts) if summary_parts else json.dumps(result)
            return str(result)
        except Exception as e:
            raise ToolError(f"Failed to check channels: {e}")

    @function_tool()
    async def list_sessions(
        self,
        context: RunContext[AppData],
    ) -> str:
        """List recent chat sessions across all channels. Shows who has been
        chatting and on which platforms.
        """
        await _say_filler(context.userdata.session, THINKING_FILLERS)
        client = context.userdata.openclaw
        try:
            result = await client.list_sessions()
            if isinstance(result, list):
                summaries = []
                for s in result[:10]:  # limit to 10 for voice
                    key = s.get("key", s.get("sessionKey", "unknown"))
                    channel = s.get("channel", "")
                    summaries.append(f"{key} ({channel})" if channel else key)
                return f"Found {len(result)} sessions. Recent: " + ", ".join(summaries)
            return str(result)
        except Exception as e:
            raise ToolError(f"Failed to list sessions: {e}")

    @function_tool()
    async def get_chat_history(
        self,
        context: RunContext[AppData],
        session_key: str,
    ) -> str:
        """Get recent messages from a specific chat session.

        Args:
            session_key: The session identifier to retrieve history for.
        """
        await _say_filler(context.userdata.session, THINKING_FILLERS)
        client = context.userdata.openclaw
        try:
            result = await client.get_chat_history(session_key, limit=5)
            if isinstance(result, list):
                messages = []
                for msg in result:
                    role = msg.get("role", "unknown")
                    content = msg.get("content", msg.get("text", ""))
                    if isinstance(content, str) and len(content) > 100:
                        content = content[:100] + "..."
                    messages.append(f"{role}: {content}")
                return "\n".join(messages) if messages else "No messages found."
            return str(result)
        except Exception as e:
            raise ToolError(f"Failed to get chat history: {e}")

    @function_tool()
    async def check_health(
        self,
        context: RunContext[AppData],
    ) -> str:
        """Check if the OpenClaw gateway is healthy and running."""
        client = context.userdata.openclaw
        try:
            result = await client.health()
            return f"OpenClaw gateway is healthy. Status: {json.dumps(result)}"
        except Exception as e:
            raise ToolError(f"Gateway health check failed: {e}")


# ── Server setup ────────────────────────────────────────────────────────

server = AgentServer()


@server.rtc_session(agent_name="liveclaw")
async def entrypoint(ctx: agents.JobContext):
    """Main voice agent entrypoint."""
    # ── Validate required environment variables ────────────────────────
    missing: list[str] = []

    gateway_url = os.environ.get("OPENCLAW_GATEWAY_URL", "ws://127.0.0.1:18789")
    gateway_token = os.environ.get("OPENCLAW_GATEWAY_TOKEN", "")

    if not gateway_token:
        missing.append(
            "OPENCLAW_GATEWAY_TOKEN — get from your OpenClaw config "
            "(~/.openclaw/openclaw.json → gateway.auth.token)"
        )
    if not os.environ.get("OPENAI_API_KEY"):
        missing.append(
            "OPENAI_API_KEY — get from https://platform.openai.com/api-keys"
        )
    if not os.environ.get("DEEPGRAM_API_KEY"):
        missing.append(
            "DEEPGRAM_API_KEY — get from https://console.deepgram.com/"
        )

    if missing:
        logger.error(
            "Missing required environment variables:\n  • %s",
            "\n  • ".join(missing),
        )
        logger.error(
            "Copy .env.example to .env.local and fill in the values, "
            "then restart the agent."
        )

    # ── Connect to OpenClaw gateway ────────────────────────────────────
    openclaw = OpenClawClient(url=gateway_url, token=gateway_token)
    try:
        await openclaw.connect()
        logger.info("Connected to OpenClaw gateway at %s", gateway_url)
    except Exception as e:
        logger.error("Failed to connect to OpenClaw gateway: %s", e)
        logger.info("Agent will start but OpenClaw tools will fail until gateway is available")

    # ── Resolve Telegram recipient to numeric chat ID if needed ────────
    global DEFAULT_TELEGRAM_RECIPIENT
    if DEFAULT_TELEGRAM_RECIPIENT and not DEFAULT_TELEGRAM_RECIPIENT.lstrip("-").isdigit():
        logger.info(
            "Resolving Telegram recipient '%s' to numeric chat ID...",
            DEFAULT_TELEGRAM_RECIPIENT,
        )
        resolved_id = await _resolve_telegram_chat_id(openclaw)
        if resolved_id:
            logger.info("Resolved Telegram chat ID: %s", resolved_id)
            DEFAULT_TELEGRAM_RECIPIENT = resolved_id
        else:
            logger.warning(
                "Could not resolve '%s' to a numeric chat ID. "
                "Make sure you've pressed /start on the Telegram bot first. "
                "You can also set DEFAULT_TELEGRAM_RECIPIENT to your numeric "
                "Telegram user ID (use /whoami on the bot).",
                DEFAULT_TELEGRAM_RECIPIENT,
            )

    session = AgentSession(
        stt=deepgram.STTv2(model="flux-general-en"),
        llm="openai/gpt-4.1-mini",
        tts="deepgram/aura-2",
        vad=silero.VAD.load(),
        turn_detection="stt",
        # Responsive interruption handling — listen when the user speaks
        min_interruption_duration=0.3,  # 300ms of speech triggers interrupt (default 0.5)
        min_interruption_words=1,  # single word like "wait" or "stop" is enough (default 0)
        false_interruption_timeout=1.5,  # 1.5s to decide if it was real speech (default 2.0)
        resume_false_interruption=True,  # auto-resume after background noise / false alarm
        userdata=AppData(openclaw=openclaw, session=None),
    )

    # Store session reference so tools can use session.say()
    session.userdata.session = session

    # ── Transcript logging ─────────────────────────────────────────────
    @session.on("conversation_item_added")
    def _on_conversation_item(ev: ConversationItemAddedEvent) -> None:
        """Log both user and agent messages for transcript visibility."""
        item = ev.item
        if not isinstance(item, ChatMessage):
            return
        role = item.role
        # text_content is a property on ChatMessage, not a method
        try:
            text = item.text_content
        except Exception:
            text = str(item.content)
        if text:
            # Truncate very long messages for log readability
            display = text[:300] + "..." if len(text) > 300 else text
            logger.info("[%s] %s", role.upper(), display)

    await session.start(
        room=ctx.room,
        agent=OpenClawVoiceAgent(),
        room_options=room_io.RoomOptions(
            close_on_disconnect=True,
            delete_room_on_close=True,
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=True,
            ),
        ),
    )


if __name__ == "__main__":
    agents.cli.run_app(server)
