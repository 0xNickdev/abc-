# Deploy: Railway (backend) + Vercel (landing)

## Backend → Railway

Репозиторий уже содержит `Procfile` и корневой `requirements.txt` — Railway (Nixpacks) подхватит их сам.

1. https://railway.app → **New Project → Deploy from GitHub repo** → `0xNickdev/abc-`.
2. В **Variables** задай:
   - `DATA_SOURCE=dex` — **реальные данные без ключей** (тренды GeckoTerminal + метрики DexScreener + mint/freeze через Solana RPC); без неё будут мок-данные;
   - `SOLANA_RPC_URL=https://mainnet.helius-rpc.com/?api-key=<твой ключ>` — свой RPC (Helius) вместо перегруженного публичного: надёжные проверки authority, балансы, отправка tx;
   - `PUBLIC_DEMO=1` — **рекомендуется для публичного инстанса**: read-only демо, все записи (buy/config/bot) отключены;
   - либо полноценный режим: `GMGN_API_KEY=...` (без него — mock-данные), опционально `GMGN_LLM_PROVIDER=claude` + `ANTHROPIC_API_KEY`.
   - **LLM-судья через DeepSeek** (дёшево, рекомендуется): `GMGN_LLM_PROVIDER=deepseek` + `DEEPSEEK_API_KEY=sk-...` (модель по умолч. `deepseek-chat`; свой эндпоинт — `LLM_BASE_URL`). Без этого судья работает на бесплатной эвристике.
   - `ENABLE_LIVE_TRADING` **не задавать** (замок реальных денег остаётся закрытым).
3. Settings → **Generate Domain** → получишь `https://<app>.up.railway.app`.

⚠️ Публичный инстанс без `PUBLIC_DEMO=1` — на свой риск: вход подписью защищает секреты и tx,
но rate-limit на юзера ещё не реализован (этап 7) — квоту оператора могут выжечь.


## Постоянные данные + автозапуск бота (Railway Volume)

Чтобы бумажный журнал/позиции пережили перезапуск контейнера и бот сам поднимался:

1. В сервисе **web → Settings → Volumes → + New Volume**, Mount path: `/data`.
2. В **Variables** добавь:
   - `ABC_DATA_DIR=/data` — журнал/позиции/фильтры пишутся на постоянный диск;
   - `BOT_AUTOSTART=n2` — после каждого рестарта бот сам стартует в N2 (бумага).
3. Railway передеплоит. С этого момента прогон непрерывный и переживает перезапуски.

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
