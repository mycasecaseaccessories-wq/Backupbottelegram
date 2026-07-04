# Telegram Backup SaaS

A small Python project that backs up Telegram chats to a private channel,
detects deleted messages and provides a control bot + REST API.

## Stack

| Layer        | Library                       |
|--------------|-------------------------------|
| Userbot      | Telethon                      |
| Control bot  | python-telegram-bot 13.15     |
| Backend API  | Flask                         |
| Database     | SQLite                        |

## Files

```
telegram-backup/
├── config.py        # env-driven configuration
├── db.py            # SQLite schema + queries
├── main.py          # Telethon userbot
├── bot.py           # Control bot (inline-button menu)
├── api.py           # Flask backend
├── export.py        # ZIP export helper
├── run.py           # Single entry point
└── requirements.txt
```

## Required secrets

Set these in Replit Secrets (or as environment variables):

| Key                | Description                                         |
|--------------------|-----------------------------------------------------|
| `API_ID`           | From <https://my.telegram.org>                      |
| `API_HASH`         | From <https://my.telegram.org>                      |
| `BOT_TOKEN`        | From [@BotFather](https://t.me/BotFather)           |
| `BACKUP_CHANNEL_ID`| Numeric id of your private backup channel           |
| `ADMIN_CHAT_ID`    | Your own Telegram numeric id (for payment approval) |

The userbot account **must be a member** of `BACKUP_CHANNEL_ID`.

## Run

```bash
python telegram-backup/run.py
```

The very first run prompts on the console for the userbot's phone number
and login code (Telethon flow). After that the session is cached in
`telegram-backup/sessions/`.

## Control bot menu

`/start` shows an inline-button menu:

* **Start Backup** / **Stop Backup** – toggle backup for your account.
* **Add Chat** – follow the prompt with `@username` to monitor a chat.
* **Logs** – last 15 captured events.
* **Export** – download a ZIP with `logs.csv`.
* **Upgrade** – send a payment screenshot, queued for admin approval.

Admin (`ADMIN_CHAT_ID`) can run:

* `/payments` – list pending payments.
* `/approve <id>` – approve a payment (promotes the user to `pro`).
* `/reject  <id>` – reject a payment.

## REST API

| Method | Path             | Body / Query                                  |
|--------|------------------|-----------------------------------------------|
| POST   | `/backup/on`     | `{"telegram_id": 123}`                        |
| POST   | `/backup/off`    | `{"telegram_id": 123}`                        |
| POST   | `/targets/add`   | `{"telegram_id": 123, "target": "@user"}`     |
| GET    | `/targets`       | `?telegram_id=123` (optional)                 |
| GET    | `/logs`          | `?telegram_id=123&limit=50`                   |
| GET    | `/export`        | `?telegram_id=123` (returns a ZIP)            |
| GET    | `/healthz`       | health check                                  |

## Database

SQLite file at `telegram-backup/backup.db`:

* `users(id, telegram_id, username, plan, backup_enabled, created_at)`
* `targets(id, user_id, target_username, target_id, added_at)`
* `logs(id, user_id, target_id, target_username, chat_id, message_id,
       message_text, action, timestamp)`
* `payments(id, user_id, screenshot_path, amount, status, created_at)`
