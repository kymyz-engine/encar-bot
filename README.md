# Encar Telegram bot

1. Создай бота у @BotFather, скопируй токен.
2. `copy .env.example .env` и впиши `BOT_TOKEN=...`
3. `python -m venv .venv && .venv\Scripts\pip install -r requirements.txt`
4. `.venv\Scripts\python bot.py`

Фильтры: марка → модель → поколение, топливо, цена (만원), год выпуска.
Данные берутся из публичного API api.encar.com.

## New-listing notifications
Press "Notify me about new listings" in the menu: the current filters are saved, existing
listings are ignored, and every 2 minutes the bot checks Encar and messages you about new ones.
Subscriptions are stored in `subs.json` and survive restarts (the bot must be running to check).

## Deploy with Docker
```bash
cp .env.example .env      # set BOT_TOKEN (optionally ENCAR_PROXY / ENCAR_DIRECT)
mkdir -p data && sudo chown 1000:1000 data   # volume for subs.json and names.json
docker compose up -d --build
docker compose logs -f
```
Update: `git pull && docker compose up -d --build`. Stop: `docker compose down`.
Run only ONE instance per bot token, otherwise Telegram returns a polling conflict.
