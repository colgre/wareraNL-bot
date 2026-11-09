# Verifier Bot

A small read-only Discord bot that lives in the **production** server. Its only job is to expose a local HTTP API so the war-guild bot can check whether a Discord user holds the Nederlander role — without the war-guild bot needing to join the production server.

## Creating the Discord application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) → **New Application**
2. **Bot** tab → copy the **Token** → put it in `.env` as `TOKEN_VERIFIER`
3. **Privileged Gateway Intents** → enable **Server Members Intent**
4. Invite to the production server (`scope=bot` only, no slash commands, no permissions):
   ```
   https://discord.com/oauth2/authorize?client_id=YOUR_VERIFIER_CLIENT_ID&scope=bot&permissions=0
   ```

## HTTP API

Listens on `VERIFIER_PORT` (default `8765`). All endpoints require `Authorization: Bearer <VERIFIER_SECRET>`.

| Endpoint | Response |
|----------|----------|
| `GET /check/{discord_id}` | `{"has_role": bool}` |
| `GET /member/{discord_id}` | `{"has_role": bool, "nickname": str \| null}` |
| `GET /nederlanders` | `{"ids": ["123", ...]}` (holders of `VERIFIER_ROLE_ID`) |
| `GET /roles/{role_id}` | `{"ids": ["123", ...]}` (holders of any role ID, e.g. cabinet roles) |

Returns `503` while the guild cache is not ready, `401` on bad/missing token.

Inside Docker the verifier is reachable at `http://verifier-bot:8765`. Outside Docker use `http://127.0.0.1:8765`.

## Running

Requires `TOKEN_VERIFIER`, `VERIFIER_SECRET`, `VERIFIER_GUILD_ID`, `VERIFIER_ROLE_ID` in `.env`.

```bash
# Recreate
docker build -t warera-nl:latest . && docker compose -f docker-compose.verifier.yml up -d --force-recreate verifier-bot

# Logs
docker compose -f docker-compose.verifier.yml logs -f verifier-bot

# Stop
docker compose -f docker-compose.verifier.yml stop verifier-bot
```
