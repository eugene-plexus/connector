"""Unit tests for `DiscordAdapter._handle_message`.

We bypass discord.py's websocket loop entirely and invoke the message
handler directly with hand-crafted fake message / author / channel
objects. The goal is to exercise the routing logic (unknown user →
pending link; known user → orchestrator forward; non-DM-non-mention
→ ignore) without needing a Discord connection.

Fake DM channels carry the `_is_dm_for_test=True` marker that
`DiscordAdapter._is_dm()` honors (in addition to the real
`isinstance(channel, discord.DMChannel)` check).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import pytest

from eugene_plexus_connector._generated.common_models import (
    AdapterEntry,
    AdapterKind,
)
from eugene_plexus_connector.adapters.base import (
    AdapterHooks,
    OrchestratorRequest,
    PendingLinkPayload,
)
from eugene_plexus_connector.adapters.discord_adapter import DiscordAdapter


@dataclass
class _FakeAuthor:
    id: int
    name: str
    display_name: str
    display_avatar: Any = None


@dataclass
class _FakeChannel:
    id: int | None = None
    name: str | None = None
    _is_dm_for_test: bool = False
    sent: list[str] = field(default_factory=list)
    history_items: list[Any] = field(default_factory=list)

    async def send(self, content: str) -> None:
        self.sent.append(content)

    def history(self, *, limit: int, before: Any | None = None) -> Any:
        items = self.history_items[:limit]

        async def _gen() -> Any:
            for item in items:
                yield item

        return _gen()


@dataclass
class _FakeMessage:
    content: str
    author: _FakeAuthor
    channel: Any
    mentions: list[Any] = field(default_factory=list)


def _hooks(
    *,
    aliases: dict[tuple[str, str], UUID] | None = None,
    orchestrator_reply: str = "blended",
) -> tuple[
    AdapterHooks, list[PendingLinkPayload], list[OrchestratorRequest]
]:
    """Build hooks that record all outbound calls into lists."""
    aliases = aliases or {}
    pending: list[PendingLinkPayload] = []
    orchestrator_calls: list[OrchestratorRequest] = []

    async def resolve(platform: str, account_id: str) -> UUID | None:
        return aliases.get((platform, account_id))

    async def file_link(payload: PendingLinkPayload) -> bool:
        pending.append(payload)
        return True

    async def send_chat(req: OrchestratorRequest) -> str:
        orchestrator_calls.append(req)
        return orchestrator_reply

    return (
        AdapterHooks(
            resolve_person=resolve,
            file_pending_link=file_link,
            send_to_orchestrator=send_chat,
        ),
        pending,
        orchestrator_calls,
    )


def _adapter(hooks: AdapterHooks, **cfg_extras: Any) -> DiscordAdapter:
    """Build a DiscordAdapter without ever calling client.start()."""
    cfg: dict[str, Any] = {"botToken": "fake", **cfg_extras}
    entry = AdapterEntry(
        name="discord-1",
        kind=AdapterKind.discord,
        adapterConfig=cfg,
        enabled=True,
    )
    adapter = DiscordAdapter.from_entry(entry, hooks)
    # Pin a fake bot-user id so mention checks behave predictably.
    adapter._client._connection.user = _FakeAuthor(  # type: ignore[assignment]
        id=999, name="eugene-bot", display_name="Eugene"
    )
    return adapter


def test_can_build_discord_adapter_from_entry() -> None:
    """Building doesn't require a real bot token validation."""
    hooks, _, _ = _hooks()
    adapter = _adapter(hooks)
    assert adapter.name == "discord-1"
    snapshot = adapter.status()
    assert snapshot.status == "disabled"


@pytest.mark.asyncio
async def test_dm_from_unknown_user_files_pending_and_replies() -> None:
    hooks, pending, orch_calls = _hooks()
    adapter = _adapter(hooks)
    channel = _FakeChannel(id=42, _is_dm_for_test=True)
    author = _FakeAuthor(id=1234, name="stranger", display_name="Stranger")
    msg = _FakeMessage(content="hi Eugene", author=author, channel=channel)

    await adapter._handle_message(msg)  # type: ignore[arg-type]

    assert len(pending) == 1
    assert pending[0].platform == "discord"
    assert pending[0].account_id == "1234"
    assert pending[0].triggering_message == "hi Eugene"
    # Unknown user MUST NOT reach orchestrator.
    assert orch_calls == []
    # The static "ask the operator" reply was sent.
    assert len(channel.sent) == 1
    assert "operator" in channel.sent[0]


@pytest.mark.asyncio
async def test_dm_from_known_user_forwards_to_orchestrator() -> None:
    person_id = uuid4()
    hooks, pending, orch_calls = _hooks(
        aliases={("discord", "1234"): person_id}
    )
    adapter = _adapter(hooks)
    channel = _FakeChannel(id=42, _is_dm_for_test=True)
    author = _FakeAuthor(id=1234, name="known", display_name="Known")
    msg = _FakeMessage(content="hi Eugene", author=author, channel=channel)

    await adapter._handle_message(msg)  # type: ignore[arg-type]

    assert pending == []
    assert len(orch_calls) == 1
    assert orch_calls[0].person_id == person_id
    assert orch_calls[0].content == "hi Eugene"
    assert orch_calls[0].source.platform == "discord"
    assert orch_calls[0].source.isDirectMessage is True
    assert channel.sent == ["blended"]


@pytest.mark.asyncio
async def test_channel_message_without_mention_is_ignored() -> None:
    """Messages in channels that don't @-mention the bot must be
    silently ignored — Eugene doesn't speak unless spoken to."""
    person_id = uuid4()
    hooks, pending, orch_calls = _hooks(
        aliases={("discord", "1234"): person_id}
    )
    adapter = _adapter(hooks)
    channel = _FakeChannel(id=42, name="general")  # not a DM
    author = _FakeAuthor(id=1234, name="known", display_name="Known")
    msg = _FakeMessage(
        content="random chat among humans",
        author=author,
        channel=channel,
        mentions=[],  # no bot mention
    )

    await adapter._handle_message(msg)  # type: ignore[arg-type]

    assert pending == []
    assert orch_calls == []
    assert channel.sent == []


@pytest.mark.asyncio
async def test_channel_mention_respects_allowlist() -> None:
    """A non-allowlisted channel that @-mentions the bot is ignored."""
    person_id = uuid4()
    hooks, pending, orch_calls = _hooks(
        aliases={("discord", "1234"): person_id}
    )
    adapter = _adapter(hooks, channelAllowlist="100,200")
    forbidden_channel = _FakeChannel(id=999, name="off-limits")
    author = _FakeAuthor(id=1234, name="known", display_name="Known")
    bot_user = adapter._client.user
    msg = _FakeMessage(
        content="hey @Eugene",
        author=author,
        channel=forbidden_channel,
        mentions=[bot_user],
    )

    await adapter._handle_message(msg)  # type: ignore[arg-type]

    assert orch_calls == []
    assert pending == []
    assert forbidden_channel.sent == []


@pytest.mark.asyncio
async def test_channel_mention_within_allowlist_forwards() -> None:
    person_id = uuid4()
    hooks, _pending, orch_calls = _hooks(
        aliases={("discord", "1234"): person_id}
    )
    adapter = _adapter(hooks, channelAllowlist="100,200")
    allowed_channel = _FakeChannel(id=100, name="dev-banter")
    author = _FakeAuthor(id=1234, name="known", display_name="Known")
    bot_user = adapter._client.user
    msg = _FakeMessage(
        content="hey @Eugene",
        author=author,
        channel=allowed_channel,
        mentions=[bot_user],
    )

    await adapter._handle_message(msg)  # type: ignore[arg-type]

    assert len(orch_calls) == 1
    assert orch_calls[0].source.channelId == "100"
    assert orch_calls[0].source.isDirectMessage is False


@pytest.mark.asyncio
async def test_self_mention_stripped_from_outbound_content() -> None:
    """The orchestrator receives clean text, not the @<bot> token."""
    person_id = uuid4()
    hooks, _pending, orch_calls = _hooks(
        aliases={("discord", "1234"): person_id}
    )
    adapter = _adapter(hooks)
    channel = _FakeChannel(id=42, _is_dm_for_test=True)
    author = _FakeAuthor(id=1234, name="known", display_name="Known")
    msg = _FakeMessage(
        content="<@999> hello there",
        author=author,
        channel=channel,
    )

    await adapter._handle_message(msg)  # type: ignore[arg-type]

    assert len(orch_calls) == 1
    assert orch_calls[0].content == "hello there"


@pytest.mark.asyncio
async def test_bot_ignores_its_own_messages() -> None:
    """Replies we post show up in on_message too — must skip."""
    hooks, pending, orch_calls = _hooks()
    adapter = _adapter(hooks)
    channel = _FakeChannel(id=42, _is_dm_for_test=True)
    bot_user = adapter._client.user
    # Build a _FakeAuthor with the same id so the "this is me" check fires.
    bot_author = _FakeAuthor(
        id=bot_user.id if bot_user is not None else 0,
        name="eugene-bot",
        display_name="Eugene",
    )
    msg = _FakeMessage(
        content="my own reply",
        author=bot_author,
        channel=channel,
    )

    await adapter._handle_message(msg)  # type: ignore[arg-type]

    assert pending == []
    assert orch_calls == []
    assert channel.sent == []
