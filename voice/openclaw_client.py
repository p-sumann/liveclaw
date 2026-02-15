"""OpenClaw Gateway WebSocket client.

Connects to the OpenClaw gateway via WebSocket using the challenge-response
handshake protocol (protocol version 3) with Ed25519 device identity for
full scope authorization.

Supports streaming agent responses via async iterators for real-time TTS.
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

import websockets
from websockets.asyncio.client import ClientConnection

logger = logging.getLogger("openclaw-client")

PROTOCOL_VERSION = 3
CLIENT_ID = "gateway-client"
CLIENT_MODE = "backend"
ROLE = "operator"
SCOPES = ["operator.admin"]


# ── Device identity helpers ─────────────────────────────────────────────


def _b64url_encode(data: bytes) -> str:
    """Base64url-encode without padding (matches OpenClaw's format)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _public_key_raw(pub: Ed25519PublicKey) -> bytes:
    """Extract the 32-byte raw public key from an Ed25519 public key."""
    return pub.public_bytes(Encoding.Raw, PublicFormat.Raw)


def _derive_device_id(pub: Ed25519PublicKey) -> str:
    """SHA-256 hex digest of the raw 32-byte public key."""
    return hashlib.sha256(_public_key_raw(pub)).hexdigest()


def _load_or_create_device_identity(
    identity_path: Path,
) -> tuple[str, Ed25519PrivateKey, Ed25519PublicKey]:
    """Load or create an Ed25519 device identity, persisted to disk.

    Returns (device_id, private_key, public_key).
    """
    if identity_path.exists():
        data = json.loads(identity_path.read_text())
        priv = Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(data["private_key_b64"])
        )
        pub = priv.public_key()
        device_id = _derive_device_id(pub)
        return device_id, priv, pub

    # Generate new key pair
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    device_id = _derive_device_id(pub)

    identity_path.parent.mkdir(parents=True, exist_ok=True)
    identity_path.write_text(
        json.dumps(
            {
                "version": 1,
                "device_id": device_id,
                "private_key_b64": base64.b64encode(
                    priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
                ).decode(),
                "created_at": time.time(),
            },
            indent=2,
        )
    )
    identity_path.chmod(0o600)
    logger.info("Created new device identity: %s", device_id)
    return device_id, priv, pub


def _build_device_auth_payload(
    *,
    device_id: str,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: list[str],
    signed_at_ms: int,
    token: str,
    nonce: str,
) -> str:
    """Build the v2 signature payload matching OpenClaw's buildDeviceAuthPayload."""
    return "|".join(
        [
            "v2",
            device_id,
            client_id,
            client_mode,
            role,
            ",".join(scopes),
            str(signed_at_ms),
            token,
            nonce,
        ]
    )


def _sign_payload(private_key: Ed25519PrivateKey, payload: str) -> str:
    """Sign payload with Ed25519 and return base64url-encoded signature."""
    sig = private_key.sign(payload.encode("utf-8"))
    return _b64url_encode(sig)


# ── Client ──────────────────────────────────────────────────────────────


class AgentStream:
    """Async iterator that yields text deltas from an OpenClaw agent run."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._full_text: str = ""
        self._done = False

    def push_delta(self, delta: str) -> None:
        """Push a text delta into the stream."""
        if delta and not self._done:
            self._full_text += delta
            self._queue.put_nowait(delta)

    def finish(self) -> None:
        """Signal that the stream is complete."""
        self._done = True
        self._queue.put_nowait(None)  # sentinel

    @property
    def full_text(self) -> str:
        """The accumulated full text so far."""
        return self._full_text

    def __aiter__(self) -> "AgentStream":
        return self

    async def __anext__(self) -> str:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


class OpenClawClient:
    """Async WebSocket client for the OpenClaw gateway with device identity."""

    def __init__(self, url: str, token: str) -> None:
        self._url = url
        self._token = token
        self._ws: ClientConnection | None = None
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._listener_task: asyncio.Task[None] | None = None
        self._connected = asyncio.Event()
        self._challenge_nonce: str | None = None

        # Active agent streams keyed by runId
        self._agent_streams: dict[str, AgentStream] = {}
        # Track which runId belongs to the latest agent request
        self._latest_run_id: str | None = None

        # Load or create device identity
        identity_dir = Path(
            os.environ.get("OPENCLAW_STATE_DIR", Path.home() / ".openclaw-voice")
        )
        identity_path = identity_dir / "device-identity.json"
        self._device_id, self._private_key, self._public_key = (
            _load_or_create_device_identity(identity_path)
        )

    async def connect(self) -> None:
        """Connect to the OpenClaw gateway and authenticate."""
        try:
            self._ws = await websockets.connect(self._url)
        except websockets.exceptions.InvalidURI:
            raise ConnectionError(
                f"Invalid gateway URL: {self._url}. "
                "Expected format: ws://host:port (e.g. ws://127.0.0.1:18789)"
            )
        except ConnectionRefusedError:
            raise ConnectionError(
                f"Connection refused at {self._url}. "
                "Is the OpenClaw gateway running? Start it with: openclaw gateway"
            )
        except OSError as e:
            raise ConnectionError(
                f"Cannot connect to {self._url}: {e}. "
                "Check OPENCLAW_GATEWAY_URL and ensure the gateway is reachable."
            )

        self._listener_task = asyncio.create_task(self._listen())

        try:
            await asyncio.wait_for(self._connected.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            raise ConnectionError(
                f"Timed out waiting for gateway handshake at {self._url}. "
                "Verify the gateway is running and the port is correct (default: 18789)."
            )

        logger.info("Connected to OpenClaw gateway at %s", self._url)

    async def _send_connect(self) -> None:
        """Send the connect request with device identity after receiving the challenge."""
        assert self._ws is not None
        assert self._challenge_nonce is not None

        signed_at_ms = int(time.time() * 1000)

        # Build and sign the auth payload
        payload = _build_device_auth_payload(
            device_id=self._device_id,
            client_id=CLIENT_ID,
            client_mode=CLIENT_MODE,
            role=ROLE,
            scopes=SCOPES,
            signed_at_ms=signed_at_ms,
            token=self._token,
            nonce=self._challenge_nonce,
        )
        signature = _sign_payload(self._private_key, payload)

        connect_req = {
            "type": "req",
            "id": str(uuid.uuid4()),
            "method": "connect",
            "params": {
                "minProtocol": PROTOCOL_VERSION,
                "maxProtocol": PROTOCOL_VERSION,
                "client": {
                    "id": CLIENT_ID,
                    "version": "1.0.0",
                    "platform": "python",
                    "mode": CLIENT_MODE,
                    "displayName": "LiveKit Voice Agent",
                },
                "role": ROLE,
                "scopes": SCOPES,
                "auth": {
                    "token": self._token,
                },
                "device": {
                    "id": self._device_id,
                    "publicKey": _b64url_encode(
                        _public_key_raw(self._public_key)
                    ),
                    "signature": signature,
                    "signedAt": signed_at_ms,
                    "nonce": self._challenge_nonce,
                },
                "caps": [],
            },
        }

        req_id = connect_req["id"]
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = future

        await self._ws.send(json.dumps(connect_req))

        try:
            result = await asyncio.wait_for(future, timeout=10.0)
            if isinstance(result, dict) and result.get("type") == "hello-ok":
                logger.info(
                    "Handshake OK — server %s, protocol %d",
                    result.get("server", {}).get("version", "?"),
                    result.get("protocol", 0),
                )
            self._connected.set()
        except asyncio.TimeoutError:
            raise ConnectionError(
                f"Timed out waiting for authentication response from {self._url}. "
                "The gateway may be overloaded or the connection was dropped."
            )
        except RuntimeError as e:
            error_msg = str(e)
            if "AUTH" in error_msg.upper() or "TOKEN" in error_msg.upper():
                raise ConnectionError(
                    f"Authentication failed: {e}. "
                    "Check that OPENCLAW_GATEWAY_TOKEN matches the token in your "
                    "OpenClaw gateway config (~/.openclaw/openclaw.json → gateway.auth.token)."
                ) from e
            raise ConnectionError(
                f"Gateway handshake failed: {e}. "
                "Check the gateway logs at ~/.openclaw/logs/gateway.log for details."
            ) from e
        except Exception as e:
            logger.error("Handshake failed: %s", e)
            self._connected.set()
            raise

    async def _listen(self) -> None:
        """Background task to listen for WebSocket messages."""
        assert self._ws is not None
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type in ("event", "evt"):
                    event_name = msg.get("event")
                    if event_name == "connect.challenge":
                        payload = msg.get("payload", {})
                        self._challenge_nonce = payload.get("nonce")
                        logger.debug(
                            "Received challenge nonce=%s", self._challenge_nonce
                        )
                        asyncio.create_task(self._send_connect())
                    elif event_name == "agent":
                        self._handle_agent_event(msg.get("payload", {}))
                    elif event_name == "chat":
                        # Suppress verbose chat events (handled via agent events)
                        pass
                    elif event_name == "tick":
                        pass  # Suppress periodic heartbeat ticks
                    else:
                        logger.debug("Event: %s", event_name)

                elif msg_type == "res":
                    req_id = msg.get("id")
                    if req_id and req_id in self._pending:
                        future = self._pending.pop(req_id)
                        if not future.done():
                            if msg.get("ok"):
                                future.set_result(msg.get("payload"))
                            else:
                                error = msg.get("error", {})
                                future.set_exception(
                                    RuntimeError(
                                        f"RPC error [{error.get('code')}]: "
                                        f"{error.get('message')}"
                                    )
                                )

                else:
                    logger.debug("Unhandled message type: %s", msg_type)

        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket connection closed")

    async def request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Send an RPC request and wait for the response."""
        if self._ws is None:
            raise RuntimeError("Not connected to gateway")

        req_id = str(uuid.uuid4())
        frame: dict[str, Any] = {
            "type": "req",
            "id": req_id,
            "method": method,
        }
        if params:
            frame["params"] = params

        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = future

        await self._ws.send(json.dumps(frame))
        return await asyncio.wait_for(future, timeout=30.0)

    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self._listener_task:
            self._listener_task.cancel()
        if self._ws:
            await self._ws.close()

    # ── Agent event handling ─────────────────────────────────────────────

    def _handle_agent_event(self, payload: dict[str, Any]) -> None:
        """Route agent streaming events to the corresponding AgentStream."""
        run_id = payload.get("runId")
        stream_type = payload.get("stream")
        data = payload.get("data", {})

        if not run_id:
            return

        # Auto-associate pending stream with first seen runId
        if run_id not in self._agent_streams and hasattr(self, "_pending_agent_stream"):
            pending = getattr(self, "_pending_agent_stream", None)
            if pending is not None:
                self._agent_streams[run_id] = pending
                self._pending_agent_stream = None
                logger.debug("Auto-associated stream with run %s", run_id[:8])

        if stream_type == "assistant":
            # Text delta from the agent
            delta = data.get("delta", "")
            if delta and run_id in self._agent_streams:
                self._agent_streams[run_id].push_delta(delta)

        elif stream_type == "lifecycle":
            phase = data.get("phase")
            if phase in ("end", "error"):
                # Agent run finished — close the stream
                if run_id in self._agent_streams:
                    stream = self._agent_streams.pop(run_id)
                    if phase == "error":
                        error_msg = data.get("error", "Agent task failed")
                        stream.push_delta(f"\n[Error: {error_msg}]")
                    stream.finish()
                    logger.debug("Agent run %s finished (phase=%s)", run_id[:8], phase)

    # ── Convenience methods ─────────────────────────────────────────────

    async def send_message(
        self,
        to: str,
        message: str,
        channel: str | None = None,
    ) -> dict[str, Any]:
        """Send a message to a user/chat on a specific channel."""
        params: dict[str, Any] = {"to": to, "message": message}
        if channel:
            params["channel"] = channel
        params["idempotencyKey"] = str(uuid.uuid4())
        return await self.request("send", params)

    async def agent_message(
        self,
        message: str,
        agent_id: str = "dev",
        session_key: str | None = None,
        deliver: bool = False,
    ) -> dict[str, Any]:
        """Send a message to the OpenClaw agent for processing."""
        params: dict[str, Any] = {
            "message": message,
            "agentId": agent_id,
            "deliver": deliver,
        }
        if session_key:
            params["sessionKey"] = session_key
        params["idempotencyKey"] = str(uuid.uuid4())
        return await self.request("agent", params)

    async def agent_message_streaming(
        self,
        message: str,
        agent_id: str = "dev",
        session_key: str | None = None,
        deliver: bool = False,
    ) -> AgentStream:
        """Send a message to the OpenClaw agent and return a streaming response.

        Returns an AgentStream that yields text deltas as they arrive.
        The stream finishes when the agent run completes.
        """
        stream = AgentStream()

        # Register a catch-all so any new runId gets this stream
        self._pending_agent_stream = stream

        params: dict[str, Any] = {
            "message": message,
            "agentId": agent_id,
            "deliver": deliver,
        }
        if session_key:
            params["sessionKey"] = session_key
        params["idempotencyKey"] = str(uuid.uuid4())

        # Fire the request (don't await the full result — we stream instead)
        result = await self.request("agent", params)

        # The RPC response may include a runId we can associate
        if isinstance(result, dict):
            run_id = result.get("runId")
            if run_id:
                self._agent_streams[run_id] = stream
                logger.debug("Registered stream for agent run %s", run_id[:8])

        return stream

    async def get_channels_status(self) -> dict[str, Any]:
        """Get the status of all configured channels."""
        return await self.request("channels.status", {"probe": True})

    async def list_sessions(self, channel: str | None = None) -> dict[str, Any]:
        """List all active sessions. Returns dict with 'sessions' key."""
        params: dict[str, Any] = {}
        if channel:
            params["channel"] = channel
        return await self.request("sessions.list", params if params else None)

    async def get_chat_history(
        self, session_key: str, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Get chat history for a session."""
        return await self.request(
            "chat.history", {"sessionKey": session_key, "limit": limit}
        )

    async def list_agents(self) -> list[dict[str, Any]]:
        """List all configured agents."""
        return await self.request("agents.list")

    async def health(self) -> dict[str, Any]:
        """Get gateway health status."""
        return await self.request("health")

