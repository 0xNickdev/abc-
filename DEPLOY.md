# Deploy: Railway (backend) + Vercel (landing)

## Backend → Railway

Репозиторий уже содержит `Procfile` и корневой `requirements.txt` — Railway (Nixpacks) подхватит их сам.

1. https://railway.app → **New Project → Deploy from GitHub repo** → `0xNickdev/abc-`.
2. В **Variables** задай:
   - `HOST=0.0.0.0` (обязательно; без неё сервер слушает только loopback)
   - `PUBLIC_DEMO=1` — **рекомендуется для публичного инстанса**: read-only демо, все записи (buy/config/bot) отключены;
   - либо полноценный режим: `GMGN_API_KEY=...` (без него — mock-данные), опционально `GMGN_LLM_PROVIDER=claude` + `ANTHROPIC_API_KEY`.
   - `ENABLE_LIVE_TRADING` **не задавать** (замок реальных денег остаётся закрытым).
3. Settings → **Generate Domain** → получишь `https://<app>.up.railway.app`.

⚠️ Публичный инстанс без `PUBLIC_DEMO=1` — на свой риск: вход подписью защищает секреты и tx,
но rate-limit на юзера ещё не реализован (этап 7) — квоту оператора могут выжечь.

## Landing → Vercel

1. https://vercel.com → **Add New → Project** → импорт `0xNickdev/abc-`.
2. **Root Directory** → `landing` (Framework Preset: Other, без build-команды — чистая статика).
3. Deploy → получишь `https://<proj>.vercel.app`.

Лендинг по умолчанию ищет бэкенд на `127.0.0.1:8000` (локальный терминал).
Чтобы кнопки лендинга ходили на Railway-инстанс, открой его с параметром один раз:

```
https://<proj>.vercel.app/?backend=https://<app>.up.railway.app
```

— адрес запомнится в localStorage браузера.

## Терминал (сам интерфейс)

Терминал раздаётся самим бэкендом: `https://<app>.up.railway.app/` — это и есть UI.
Vercel нужен только для маркетингового лендинга.
