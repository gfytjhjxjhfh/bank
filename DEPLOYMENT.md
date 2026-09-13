# Deployment checklist

Do not deploy the current local data files. Create a new empty production database or attach a persistent disk containing `bank.sqlite3`.

## Hosting settings

Use a hosting provider that supports Python and persistent storage. Set the start command to:

```text
python game.py
```

Set these environment variables:

```text
HOST=0.0.0.0
PORT=<provided by the host>
COOKIE_SECURE=1
PUBLIC_BASE_URL=https://your-domain.example
```

The provider must terminate HTTPS and forward requests to the Python process. Do not expose the Python process directly without HTTPS.

## Secrets

Set Discord and email credentials in the provider's secret manager. Do not put them in source files or commit them to Git:

- `DISCORD_BOT_TOKEN`
- `DISCORD_CLIENT_ID`
- `DISCORD_CLIENT_SECRET`
- `DISCORD_REDIRECT_URI`
- `SMTP_HOST`
- `SMTP_PORT`
- `SMTP_USER`
- `SMTP_PASSWORD`

Update the Discord redirect URI to the public HTTPS URL before enabling Discord login.

## Important

- Attach persistent storage for `bank.sqlite3`, otherwise account data can disappear on redeploy.
- Keep `users.json`, `groups.json`, `messages.json`, `profile_images.json`, and `ip_logs.jsonl` out of Git.
- Test registration, login, password reset, groups, messages, and logout on the public HTTPS URL before sharing it.
- This is still a small custom application, not a production banking system. Do not process real money or sensitive identity documents with it.
