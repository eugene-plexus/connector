# eugene-plexus / connector

Connector component for [Eugene Plexus](https://github.com/eugene-plexus): bridges external chat platforms (Discord first) to the orchestrator's bicameral chat surface. The first **external sense organ** in Eugene's anatomy — the boundary between Eugene's interior life and the outside world.

## Architecture

Same shape as `hemisphere-driver`: one FastAPI service hosting a registry of platform-specific adapters. Each operator-configured adapter runs its own loop (Discord Gateway WS, Slack RTM, etc.), normalizes inbound messages, calls the orchestrator's `/v1/chat`, and delivers responses back through the platform's API.

### Flow

1. Adapter receives a platform message.
2. Resolves the platform user to a `personId`:
   - If a `PlatformAlias` exists → use the linked `personId`.
   - Otherwise → file a `PendingIdentityLink` with identity, reply on the platform with an "ask the operator to authorize" message, **STOP**.
3. Build an `IncomingMessage` and POST `/v1/chat` on the orchestrator.
4. Deliver the response back through the platform.

## Adapters

| Adapter | Status | Notes |
|---|---|---|
| `discord` | v0.2 | DMs + channel mentions; one bot per Discord server |
| `slack` | v0.3+ | |
| `matrix` | v0.3+ | |
| `telegram` | v0.3+ | |
| `gmail` | v0.3+ | |

## Running

```bash
EUGENE_PLEXUS_CONNECTOR_CONFIG_FILE=./connector.yaml \
EUGENE_PLEXUS_CONNECTOR_BIND_PORT=8085 \
eugene-plexus-connector
```

Default port is 8085. Auth (signing key, service token, master key) is supplied by the watchdog at spawn time; standalone runs without a watchdog use a no-auth dev mode.

## Discord adapter (v0.2)

The Discord adapter uses [discord.py](https://discordpy.readthedocs.io/). Operator supplies a bot token via `PATCH /v1/adapters/discord-1`'s `adapterConfig.botToken`. The adapter listens for:

- Direct messages
- `@<bot>` mentions in channels the operator has allowlisted

Unknown users → pending identity link, no orchestrator call. Known users → forwarded as a normal chat turn with `MessageSource{platform="discord", channelId, isDirectMessage}`.

End-to-end setup walkthrough (creating the Discord application, getting a token, inviting the bot, wiring it into Eugene, approving the first user): **[docs/discord-setup.md](docs/discord-setup.md)**.

## License

Apache 2.0.
