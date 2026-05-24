# Discord adapter — operator runbook

End-to-end walkthrough for wiring a Discord bot to your Eugene Plexus install. From "I have a running orchestrator" to "Eugene replies to my DMs and channel mentions."

**Estimated time:** 15–25 minutes, mostly waiting on Discord's developer portal.

**You'll need:**
- A running Eugene Plexus install with the `connector` component up. Verify with `curl http://127.0.0.1:8085/healthz` (or whatever port your watchdog assigned).
- Operator access to the install (i.e. you can log into the Eugene Plexus UI).
- A Discord account with permission to create bots and to add bots to at least one server.

---

## 1. Create the Discord application

1. Go to <https://discord.com/developers/applications>.
2. Click **New Application** in the top-right.
3. Name it whatever you want the bot to be called publicly. Click **Create**.

The application's name and avatar are what users will see in Discord. You can change either at any time from the application's **General Information** page.

## 2. Turn the application into a bot

The application page has a sidebar. Click **Bot**.

### Get the token

1. Click **Reset Token** (or **View Token** if it's a fresh application).
2. Copy the token immediately. **Discord shows it exactly once** — if you close the dialog, you'll have to reset and copy again.
3. Treat it like a password. It's the entire authentication for the bot; anyone with the token can act as the bot.

Eugene Plexus stores this token **encrypted at rest** once you've set a passphrase via the first-run wizard. If you're running an unauthenticated dev install, it's stored as plaintext in `connector.yaml`.

### Enable Message Content Intent

This is the bit that trips up most operators:

1. Scroll down to **Privileged Gateway Intents**.
2. Toggle **Message Content Intent** ON.
3. Save.

Without this intent, the bot's websocket connection succeeds but every incoming message body arrives empty — Eugene will see mentions but won't be able to read them. Symptom: bot connects fine, never responds to anything.

The other two intents Eugene needs (`Guild Messages`, `DM Messages`) are non-privileged and don't need toggling.

## 3. Invite the bot to a server

You need a server where you have **Manage Server** permission. A throwaway server you create yourself is fine for the initial smoke test.

1. In the application sidebar, click **OAuth2** → **URL Generator**.
2. Under **Scopes**, check `bot`.
3. Under **Bot Permissions**, check:
   - **Read Messages/View Channels** — so it can see channels it's mentioned in.
   - **Send Messages** — so it can reply.
   - **Read Message History** — so it can fetch the prior N messages as channel context (see §6).
4. Copy the **Generated URL** at the bottom of the page.
5. Open the URL in a new tab, pick the server, click **Authorize**.

The bot now shows up in the server's member list. It's offline because nothing's running it yet.

## 4. Wire the adapter into Eugene Plexus

Two paths:

### Option A — During the first-run wizard

If you haven't completed the wizard yet, the **Connectors** screen (the last screen) asks whether to set up Discord. Pick **Set up a Discord bot**, paste the token, optionally set:

- **Adapter name** — defaults to `discord`. Pick something memorable if you'll eventually run more than one Discord bot.
- **Allowed channel IDs** — comma-separated list (see §6 for what's allowed).

Click **Start**. The wizard creates the adapter via `POST /v1/adapters`, the connector loads it, the bot comes online.

### Option B — Post-setup, via the Config UI

1. Open the UI → **Config** → **Connector** tab → **Adapters** sub-tab.
2. Click **Add adapter**.
3. **Kind:** `discord`. (For v0.2 this is the only kind; v0.3+ adds slack/matrix/telegram/gmail.)
4. **Name:** any unique label.
5. Under the adapter's config:
   - **Bot token:** paste the token from §2.
   - **Channel allowlist:** see §6.
   - **Channel-context message count:** how many recent messages before a mention to include as grounding context (default 10, 0 disables).
6. Click **Test** to confirm the token authenticates against Discord's REST API before you commit.
7. Click **Save**.

The adapter status badge transitions through `starting` → `connected` once Discord's websocket handshake completes. If it parks on `error`, see §8.

## 5. Claim yourself as the first user

Eugene doesn't recognize anyone on Discord by default — every Discord user is an unknown to him until linked to a `personId`. This is intentional spoof resistance: a stranger can't just DM the bot and assume the operator's identity.

The first time you DM the bot:

> **You:** hey
>
> **Bot:** Hi — I don't recognize you yet. Ask the operator to authorize this link from the Eugene Plexus UI.

The bot does **NOT** invoke the orchestrator for unknown users — that conversation never reaches Eugene's deliberation loop. It's a flat refusal until the operator approves.

In the background, the connector filed a **PendingIdentityLink** on the identity component recording your Discord account ID, handle, display name, and the message that triggered the link.

### Approve the link

Open the UI → **Config** → **Identity** tab → **Pending links** sub-tab. The pending link from your DM appears as a row showing the platform (`discord`), your Discord display name or handle, and the message that triggered the link.

Click the row to expand it. You'll see two approval modes:

- **Create a new person** — used for everyone other than yourself. Type the display name and an optional relationship note ("my wife", "dev-banter channel regular"). The note is surfaced into Eugene's per-hemisphere prompts as top-level relationship context, so make it useful.
- **Alias onto an existing person** — used to claim YOUR OWN Discord identity into your operator Person record (so Eugene's per-person memory stays unified across Discord and the local UI). Pick the operator entry from the dropdown.

Click **Approve**. The pending count on the Pending Links tab updates immediately.

You can also click **Reject** if the request was a mistake (or spam). The rejection is final — if the Discord user messages the bot again, a fresh pending link gets filed.

DM the bot again. It now recognizes you and routes the message through the bicameral loop.

#### Fallback: approving via curl

If the UI isn't reachable (you're SSH'd in, the UI dev server is down, etc.), the same actions are available via the identity API:

```bash
# List pending links
curl -H "Authorization: Bearer $OPERATOR_TOKEN" \
  http://127.0.0.1:8084/v1/identity/links/pending

# Approve — alias onto an existing person:
curl -X POST \
  -H "Authorization: Bearer $OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"linkAsPersonId": "<personId>"}' \
  http://127.0.0.1:8084/v1/identity/links/pending/<link-id>/approve

# OR create a new person record:
curl -X POST \
  -H "Authorization: Bearer $OPERATOR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"displayName": "Sarah", "relationshipNote": "high school friend"}' \
  http://127.0.0.1:8084/v1/identity/links/pending/<link-id>/approve

# Reject:
curl -X POST \
  -H "Authorization: Bearer $OPERATOR_TOKEN" \
  http://127.0.0.1:8084/v1/identity/links/pending/<link-id>/reject
```

## 6. Channel allowlist + DM semantics

- **DMs are always honored** — regardless of the channel allowlist, a known user's DM always reaches the orchestrator. The allowlist only restricts where `@-mentions` are honored.
- **Empty allowlist = all channels** — leave the field blank and the bot responds to @-mentions in any channel it can see.
- **Allowlist is a list of channel IDs** (not channel names). Get a channel's ID by right-clicking it in Discord with Developer Mode enabled (`User Settings → Advanced → Developer Mode`), then **Copy Channel ID**.
- **Channel context** — for an @-mention in a channel, the adapter pulls the prior N messages (default 10) before the mention as grounding context. These messages are **not persisted to Eugene's memory** — they're one-shot prompt-side context. Only the mention itself and Eugene's reply get stored.

## 7. Smoke-test checklist

1. **Bot is online in Discord member list** — confirms the websocket connected.
2. **Adapter status badge in the UI is `connected`** — confirms the connector sees the same.
3. **DM "hey" from your own account, get the unknown-user reply** — confirms inbound messages are reaching `_handle_message`.
4. **Approve the pending link via the identity API** — confirms the identity component is wired and the operator path works.
5. **DM "hey" again, get a real Eugene response** — confirms end-to-end: connector → identity (resolve) → orchestrator (bicameral loop) → connector (deliver). This is the validating turn.
6. **Optionally** — mention the bot in an allowlisted channel, confirm the channel-context lookback works.

## 8. Troubleshooting

**Bot is offline / status stays at `error`**: check the adapter's `lastError` from the UI's Adapters list, or `GET /v1/adapters` directly. Common causes:

- Invalid token — the token was regenerated in the Discord portal and the old one is in your config. Reset, paste new, save.
- Network egress blocked — discord.py needs outbound 443 to `gateway.discord.gg`. Corporate firewall? VPN egress?

**Bot connects but never responds to messages**: almost always **Message Content Intent** in §2 wasn't toggled. The websocket arrives, message events fire, but `message.content` is empty. Discord's privileged-intents page is the fix.

**Bot responds to DMs but ignores @-mentions in a channel**: channel ID isn't in the allowlist, or the bot doesn't have **Read Messages** permission on that channel. Check the bot's role / permission overrides on the channel.

**Bot keeps replying with "I don't recognize you yet"**: the pending link wasn't approved, or it was approved but the alias landed on the wrong `personId`. Verify with `GET /v1/identity/persons/{id}` — the `aliases` array should include `{platform: "discord", accountId: "<your discord id>"}`.

**Channel mention works but Eugene doesn't see the channel context**: `channelContextLimit` is 0, or the bot doesn't have **Read Message History** permission on that channel. Defaults are 10 + Read Message History granted by the OAuth invite link in §3.

## 9. v0.2 limitations

- **One conversation per message**: each inbound Discord message starts a fresh conversation in Eugene's memory. v0.3 adds threading so a back-and-forth in a single channel groups into one conversation.
- **No slash commands** — only natural-language DMs and @-mentions.
- **No status / typing indicator** — Eugene doesn't show "is typing…" while the bicameral loop runs.
- **One adapter per Discord application** — if you want multiple Discord bots (e.g. one per server with different personas), create multiple Discord applications and configure them as separate adapters in the UI.

## 10. Where things live (for debugging)

- **Connector logs** — wherever the watchdog routes them. On a stock VS Code-launched testbed, they interleave with the watchdog's own stdout in the watchdog terminal.
- **Adapter persistence** — `connector.yaml` adjacent to the connector's config file. Adapter entries (including encrypted `adapterConfig`) live in the `adapters` array.
- **Pending links** — identity component's store. `GET /v1/identity/links/pending` shows the operator-pending queue.
- **Person aliases** — once approved, `Person.aliases[]` carries the `{platform, accountId}` pair. Visible via `GET /v1/identity/persons`.
