"""Discord adapter — discord.py integration.

Listens for:
  - Direct messages to the bot user
  - `@<bot>` mentions in allowlisted channels

Flow for each inbound message:
  1. Resolve the Discord user to a Eugene `personId` via the hooks.
     If unknown → file a PendingIdentityLink + reply on-platform with
     the static "ask the operator" message. STOP.
  2. Build the `OrchestratorRequest` (personId, content, source,
     optional channelContext for channel mentions).
  3. Send to orchestrator via the hook; deliver the response back to
     the source channel / DM thread.

The platform connection runs in a background asyncio task so the
adapter's `start()` returns promptly. `stop()` triggers the discord
client's graceful close.

**v0.2 live-Discord testing is operator-driven**: this module ships
with unit tests that exercise the message-handling logic against
fakes; full validation requires a real bot token + Discord server,
which is documented in the README runbook.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import discord

from .._generated.common_models import (
    AdapterEntry,
    ChannelContextEntry,
    ConfigField,
    ConfigValueType,
    MessageSource,
)
from .base import (
    Adapter,
    AdapterError,
    AdapterHooks,
    AdapterStatusSnapshot,
    OrchestratorRequest,
    PendingLinkPayload,
    PlatformIdentity,
    TestResult,
)

if TYPE_CHECKING:
    pass

log = logging.getLogger(__name__)


# Static reply to an unknown Discord user. Kept terse — the operator
# decides whether to authorize the link, this just tells the human
# what's happening. The wording explicitly says "your account" rather
# than "this link" (which several operators read as referring to the
# Discord connector / a URL rather than the identity-binding link
# being filed for approval).
_UNKNOWN_USER_REPLY = (
    "Hi — I don't recognize you yet. The operator needs to approve "
    "your account in the Eugene Plexus UI (Identity → Pending) before "
    "I can chat with you."
)


# How many prior channel messages to include as grounding context on a
# channel mention. Configurable per-adapter via `channelContextLimit`.
_DEFAULT_CHANNEL_CONTEXT_LIMIT = 10


@dataclass
class _DiscordAdapterConfig:
    bot_token: str
    channel_allowlist: list[str] = field(default_factory=list)
    """Discord channel IDs the bot is allowed to respond to on mention.
    Empty list = all channels. DM behavior is independent (always on)."""
    channel_context_limit: int = _DEFAULT_CHANNEL_CONTEXT_LIMIT


class DiscordAdapter:
    """discord.py-backed adapter."""

    def __init__(
        self,
        *,
        name: str,
        config: _DiscordAdapterConfig,
        hooks: AdapterHooks,
    ) -> None:
        self.name = name
        self._config = config
        self._hooks = hooks

        intents = discord.Intents.default()
        intents.message_content = True
        intents.dm_messages = True
        intents.guild_messages = True
        self._client = discord.Client(intents=intents)

        self._task: asyncio.Task[None] | None = None
        self._status: str = "disabled"
        self._connected_at: datetime | None = None
        self._last_error: str | None = None
        self._platform_identity: PlatformIdentity | None = None

        # Wire discord.py event handlers. Using closures rather than
        # subclassing Client keeps the adapter testable — handlers can
        # be invoked directly via `_handle_message()` without booting
        # the websocket.
        @self._client.event
        async def on_ready() -> None:
            user = self._client.user
            if user is not None:
                self._platform_identity = PlatformIdentity(
                    platform_account_id=str(user.id),
                    display_name=str(user),
                )
            self._status = "connected"
            self._connected_at = datetime.now(UTC)
            self._last_error = None
            log.info(
                "discord adapter %r connected as %s",
                self.name,
                self._platform_identity.display_name if self._platform_identity else "?",
            )

        @self._client.event
        async def on_disconnect() -> None:
            # discord.py emits on_disconnect on transient
            # reconnections too; keep `connected` if we can still
            # see a populated user.
            if self._client.is_closed():
                self._status = "disconnected"
                log.info("discord adapter %r disconnected", self.name)

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            try:
                await self._handle_message(message)
            except Exception:
                # Never let an exception bubble into discord.py's
                # event loop — it terminates the client. Log + carry on.
                log.exception(
                    "discord adapter %r failed on_message handler", self.name
                )

    # -------- Construction from spec types --------

    @classmethod
    def field_specs(cls) -> list[ConfigField]:
        return [
            ConfigField(
                key="botToken",
                label="Bot token",
                description=(
                    "Discord bot token from the application portal. "
                    "Stored encrypted on disk when the master key is "
                    "available."
                ),
                category="auth",
                valueType=ConfigValueType.secret,
                required=True,
                sensitive=True,
                requiresRestart=True,
            ),
            ConfigField(
                key="channelAllowlist",
                label="Channel allowlist (channel IDs)",
                description=(
                    "Comma- or newline-separated Discord channel IDs "
                    "the bot will respond to on @-mention. Leave "
                    "empty to allow all channels. DMs are always "
                    "honored regardless."
                ),
                category="behavior",
                valueType=ConfigValueType.string,
                default="",
            ),
            ConfigField(
                key="channelContextLimit",
                label="Channel-context message count",
                description=(
                    "For channel mentions, how many recent messages "
                    "before the mention to include as grounding "
                    "context. These messages are NOT persisted to "
                    "Eugene's memory — only the mention/reply pair "
                    "Eugene participated in is stored."
                ),
                category="behavior",
                valueType=ConfigValueType.integer,
                default=_DEFAULT_CHANNEL_CONTEXT_LIMIT,
                minimum=0,
                maximum=50,
            ),
        ]

    @classmethod
    def from_entry(cls, entry: AdapterEntry, hooks: AdapterHooks) -> DiscordAdapter:
        cfg = entry.adapterConfig or {}
        bot_token = cfg.get("botToken")
        if not isinstance(bot_token, str) or not bot_token:
            raise AdapterError(
                status_code=400,
                detail="discord adapter requires `adapterConfig.botToken`.",
            )
        allowlist_raw = cfg.get("channelAllowlist") or ""
        if isinstance(allowlist_raw, str):
            channels = [
                c.strip()
                for c in allowlist_raw.replace(",", "\n").splitlines()
                if c.strip()
            ]
        elif isinstance(allowlist_raw, list):
            channels = [str(c).strip() for c in allowlist_raw if str(c).strip()]
        else:
            channels = []
        limit_raw = cfg.get("channelContextLimit", _DEFAULT_CHANNEL_CONTEXT_LIMIT)
        try:
            limit = max(0, min(50, int(limit_raw)))
        except (TypeError, ValueError):
            limit = _DEFAULT_CHANNEL_CONTEXT_LIMIT

        return cls(
            name=entry.name,
            config=_DiscordAdapterConfig(
                bot_token=bot_token,
                channel_allowlist=channels,
                channel_context_limit=limit,
            ),
            hooks=hooks,
        )

    # -------- Lifecycle --------

    async def start(self) -> None:
        """Connect to Discord in a background task and return promptly.

        discord.py's `client.start(token)` is a long-running coroutine
        — we launch it as a Task so route handlers don't block.
        """
        if self._task is not None and not self._task.done():
            return
        self._status = "starting"
        self._last_error = None

        async def _run() -> None:
            try:
                await self._client.start(self._config.bot_token)
            except Exception as e:
                self._status = "error"
                self._last_error = str(e)
                log.exception("discord adapter %r failed to start", self.name)

        self._task = asyncio.create_task(_run(), name=f"discord-{self.name}")

    async def stop(self) -> None:
        """Disconnect and join the background task. Idempotent."""
        if self._client and not self._client.is_closed():
            try:
                await self._client.close()
            except Exception:
                log.exception("discord adapter %r failed clean close", self.name)
        if self._task is not None:
            # Best-effort shutdown; if discord.py doesn't return
            # cleanly we move on rather than blocking the connector
            # process's lifespan close.
            with contextlib.suppress(TimeoutError, Exception):
                await asyncio.wait_for(self._task, timeout=10.0)
            self._task = None
        self._status = "disconnected"

    def status(self) -> AdapterStatusSnapshot:
        connected_iso = (
            self._connected_at.isoformat() if self._connected_at else None
        )
        return AdapterStatusSnapshot(
            status=self._status,
            connected_at=connected_iso,
            last_error=self._last_error,
            platform_identity=self._platform_identity,
        )

    async def test(self) -> TestResult:
        """Fetch the bot's own user record from Discord's REST API.

        Uses a one-shot HTTP client rather than the persistent
        websocket connection so this works whether or not `start()`
        has been called.
        """
        intents = discord.Intents.default()
        scratch = discord.Client(intents=intents)
        try:
            # `login` does just the REST auth handshake — no websocket
            # connection. Perfect for a Test Connection check.
            await scratch.login(self._config.bot_token)
            user = scratch.user
            if user is None:
                return TestResult(
                    ok=False,
                    detail="Discord login succeeded but returned no user record.",
                )
            return TestResult(
                ok=True,
                detail=f"Authenticated as Discord user {user} (id={user.id}).",
            )
        except discord.LoginFailure as e:
            return TestResult(ok=False, detail=f"Discord login rejected: {e}")
        except Exception as e:
            return TestResult(ok=False, detail=f"Discord test failed: {e!r}")
        finally:
            with contextlib.suppress(Exception):
                await scratch.close()

    # -------- Message handling --------

    @staticmethod
    def _is_dm(channel: Any) -> bool:
        """Whether `channel` is a Discord direct message channel.

        Factored out for testability — tests inject fakes that don't
        share discord.py's `DMChannel` class. The runtime check
        prefers `isinstance(channel, discord.DMChannel)` and falls
        back to discord.py's channel-type marker.
        """
        if isinstance(channel, discord.DMChannel):
            return True
        # Some discord.py versions / mock objects expose `.type` as
        # `ChannelType.private` for DMs.
        return getattr(channel, "_is_dm_for_test", False)

    async def _handle_message(self, message: discord.Message) -> None:
        """Decide what to do with one Discord message.

        Pure async — invokable directly from tests without spinning
        up the discord.py websocket loop. The discord.py event
        handler is just a thin wrapper that calls this.
        """
        # Ignore our own messages — replies we post show up in the
        # `on_message` stream too.
        if self._client.user is not None and message.author.id == self._client.user.id:
            return
        # Skip non-DM, non-mention messages.
        is_dm = self._is_dm(message.channel)
        is_mention = (
            self._client.user is not None
            and self._client.user in message.mentions
        )
        if not (is_dm or is_mention):
            return
        # Channel-mention allowlist enforcement (DMs ignore allowlist).
        if is_mention and not is_dm and self._config.channel_allowlist:
            check_id = str(message.channel.id) if message.channel else ""
            if check_id not in self._config.channel_allowlist:
                return

        platform = "discord"
        account_id = str(message.author.id)
        person_id = await self._hooks.resolve_person(platform, account_id)

        # Strip the bot mention from the message content for cleanliness.
        content = self._strip_self_mention(message.content)

        if person_id is None:
            # Unknown user — file pending link, reply on-platform, STOP.
            await self._hooks.file_pending_link(
                PendingLinkPayload(
                    platform=platform,
                    account_id=account_id,
                    handle=getattr(message.author, "name", None),
                    display_name=getattr(message.author, "display_name", None),
                    avatar_url=(
                        str(message.author.display_avatar.url)
                        if getattr(message.author, "display_avatar", None)
                        else None
                    ),
                    triggering_message=content,
                )
            )
            await message.channel.send(_UNKNOWN_USER_REPLY)
            return

        # Known user — gather context and send to orchestrator.
        channel_id: str | None = (
            str(message.channel.id) if not is_dm else None
        )
        channel_name: str | None = (
            getattr(message.channel, "name", None) if not is_dm else None
        )
        channel_context = (
            await self._collect_channel_context(message) if (is_mention and not is_dm) else None
        )

        source = MessageSource(
            platform=platform,
            channelId=channel_id,
            channelName=channel_name,
            isDirectMessage=is_dm,
        )

        # Show the Discord-native "is typing…" indicator while the
        # bicameral loop runs. Bicameral turns can take 5-30s; without
        # this the user is left wondering if the bot is dead. The
        # indicator auto-clears when we exit the context manager (or
        # send a message, whichever comes first), so there's no
        # cleanup to worry about if send_to_orchestrator raises.
        #
        # Orchestrator failures (upstream LLM rate-limit, network
        # blip, hemisphere down, etc.) MUST surface as a Discord
        # message — `on_message`'s blanket try/except above would
        # otherwise log the error and leave the user staring at a
        # stopped typing indicator with no reply ever arriving.
        # Operator gets the full stack trace via log.exception; the
        # Discord user gets a terse, friendly fallback.
        reply_text: str
        async with message.channel.typing():
            try:
                reply_text = await self._hooks.send_to_orchestrator(
                    OrchestratorRequest(
                        person_id=person_id,
                        content=content,
                        conversation_id=None,  # v0.2: each message starts a fresh conversation
                        source=source,
                        channel_context=channel_context,
                    )
                )
            except Exception as e:  # noqa: BLE001 — surface ANY failure
                log.exception(
                    "orchestrator call failed for message from %s "
                    "(person_id=%s); replying with fallback",
                    getattr(message.author, "name", "<unknown>"),
                    person_id,
                )
                reply_text = (
                    "Sorry — I hit a snag on that one. Try again in a "
                    f"moment? (Operator: check the connector log; "
                    f"{type(e).__name__})"
                )

        await message.channel.send(reply_text)

    def _strip_self_mention(self, content: str) -> str:
        """Remove the bot's own @-mention from message text.

        Discord renders mentions as `<@123>` or `<@!123>` (the latter
        on legacy clients). Both forms are stripped — what we hand to
        the orchestrator is the human's natural-language content
        without the @Eugene noise.
        """
        if self._client.user is None:
            return content
        uid = self._client.user.id
        for token in (f"<@{uid}>", f"<@!{uid}>"):
            content = content.replace(token, "").strip()
        return content

    async def _collect_channel_context(
        self, message: discord.Message
    ) -> list[ChannelContextEntry]:
        """Pull the last N messages before the mention as grounding
        context. NOT persisted to Eugene's memory — connector adapters
        ship these as one-time prompt-side context only."""
        limit = self._config.channel_context_limit
        if limit <= 0:
            return []
        entries: list[ChannelContextEntry] = []
        try:
            async for prior in message.channel.history(limit=limit, before=message):
                # Skip the triggering message itself and our own messages.
                if (
                    self._client.user is not None
                    and prior.author.id == self._client.user.id
                ):
                    continue
                entries.append(
                    ChannelContextEntry(
                        author=str(
                            getattr(prior.author, "display_name", prior.author)
                        ),
                        content=prior.content,
                        timestamp=prior.created_at,
                    )
                )
        except Exception:
            log.exception(
                "discord adapter %r failed channel history fetch", self.name
            )
        # discord.py yields newest-first; reverse so context reads
        # chronologically in the prompt.
        return list(reversed(entries))


# Structural Protocol check at module-load time. Keeps drift between
# the Protocol and the concrete implementation caught at import.
_: Adapter
def _check_protocol() -> Adapter:
    """Type-only protocol check — never called."""
    raise NotImplementedError


_ann_anchor: Any = DiscordAdapter  # silences "unused" if the protocol var is removed
