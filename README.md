<div align="center">

# abc. — AI terminal for Solana memecoins

**Screening → scoring → LLM judge → a human (or the bot) presses the button.**
Non-custodial: your private key never leaves Phantom.

</div>

---

## What is this

**abc.** is a local trading terminal for memecoins (Solana, pump.fun and others) built on top of the GMGN OpenAPI. It doesn't "post signals to Telegram" — it runs every token from the trending list through a funnel of hard checks and hands you a short list of candidates with an explanation of why each one passed. Buying is either manual one-click or via an autonomous bot (everything is **paper by default**; real money is behind separate locks).

### Screening funnel

```
GMGN trending list (100 tokens)
  → Hard gates            scam filter: mint/freeze authority, bundler%,
                          dev holdings, top10 concentration, blacklists
  → Tunable filters       liquidity, volume, mcap, age, snipers,
                          wash-trading (each user has their own values)
  → Scoring               5m/1h momentum, buy pressure, turnover,
                          smart-money consensus — sort and cut
  → LLM judge             explains survivors only (Claude or a free
                          heuristic), "golden dog vs exit liquidity"
  → Your call             Buy button / autonomous bot
```

### Key features

- **Multi-user.** Every wallet (pubkey) gets its own session: filters, positions, mode, strategy, bot. The market layer is shared (operator's key), so API quota doesn't multiply with users.
- **Sign-in with wallet.** Challenge → Phantom `signMessage` → the server verifies the ed25519 signature. Secrets and transaction building require sign-in.
- **Non-custodial execution.** The server builds the swap via Jupiter v6; **you** sign and send it in Phantom. The private key never leaves the browser at any step.
- **3 named strategies** — per-user selection, instant switch:

  | Strategy | Style | Entry bar |
  |---|---|---|
  | ABC Alpha v1 | balanced | ≥2 smart wallets in, 0–45 min window |
  | ABC Sniper | conservative | ≥3 wallets, buys ≥60%, tighter stop, fuse after 2 losses |
  | ABC Degen | aggressive | 1 wallet is enough, up to 90 min window, targets up to 8x |

- **Autonomous bot** (paper-first). Buys only candidates that passed all gates **and** the strategy trigger; exits: hard stop → escape signals → trailing → TP ladder; discipline: max new entries per tick, consecutive-loss fuse, daily loss cap.
- **Smart-money signal** (not copy-trading): a private list of tracked wallets — matches are highlighted with ★ in the table (names in the tooltip); KOLs from GMGN data get 👑.
- **Twitter/KOL opt-in.** Each user connects **their own** Twitter API v2 Bearer token (KOL/𝕏 tab): a button in the buy modal shows fresh mentions of the contract address and top authors. Your token = your quota; stored server-side with chmod 600, never echoed back.
- **Solana-correct checks.** SPL tokens have no transfer-tax mechanism — tax gates apply to EVM chains only. Solana's "honeypot" is an unrenounced freeze authority: flagged red as `FREEZE!`.
- **Feedback flywheel.** Every decision is logged to `trade_decisions.jsonl`; `backtest.py` computes realized PnL / winrate / R from your own history — strategies are validated on your own data.
- **Prompt-injection hardening.** The LLM never sees raw token names — only a sanitized symbol plus numeric features.

## Quick start

```bash
cd aitrader
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python app.py            # → http://127.0.0.1:8000
```

With no keys it runs in demo mode (mock data) — click around freely. Real market data:

```bash
# ~/.config/gmgn/.env
GMGN_API_KEY=...         # GMGN OpenAPI key (market data)
```

Real LLM judge (optional; otherwise a free deterministic heuristic):

```bash
GMGN_LLM_PROVIDER=claude ANTHROPIC_API_KEY=... python app.py
```

Step-by-step beginner guide (ru): [aitrader/КАК_ЗАПУСТИТЬ.md](aitrader/КАК_ЗАПУСТИТЬ.md). Product design & roadmap (ru): [aitrader/ДИЗАЙН_ПРОДУКТА.md](aitrader/ДИЗАЙН_ПРОДУКТА.md). Technical details: [aitrader/README.md](aitrader/README.md) (en/zh).

## Safety (the locks)

Real money is disabled behind **three independent locks** — remove them deliberately:

1. `ENABLE_LIVE_TRADING=1` — env var; without it any mode = SHADOW (paper);
2. LIVE mode — toggled manually in the UI, always resets to SHADOW after restart;
3. Non-custodial path: the server builds each transaction, but **you confirm it in Phantom** — nothing leaves silently.

Also: the server binds to `127.0.0.1` only; operator keys live in `~/.config/gmgn/.env` (outside the repo); user secrets (Twitter) are chmod 600 and never returned by the API; the operator credentials panel is visible only with `ABC_ADMIN=1`.

## Architecture

```
aitrader/
├── app.py          FastAPI: funnel, gates, sessions, risk, auth, all endpoints
├── strategy.py     strategy registry (entry triggers + discipline presets)
├── bot.py          autonomous loop (DI, per-user, paper-first)
├── execution.py    Jupiter v6: building unsigned swap transactions
├── kol.py          Twitter API v2: contract mentions (5-min cache)
├── wallets.py      private smart-money list (signal, not copy-trading)
├── backtest.py     funnel + realized PnL from the local decision journal
├── static/         the terminal (single HTML, no build step; web3.js for Phantom)
└── tests/          89 tests: gates, risk, strategies, multi-user, auth, tx
```

Per-user state lives in `outputs/users/<pubkey>/` (git-ignored). The trending list is cached per chain — ten open tabs won't burn the quota. Landing page lives in [landing/](landing/).

## Tests

```bash
cd aitrader && . .venv/bin/activate
python -m pytest tests/ -q     # 89 passed
ruff check .                   # clean
```

## Roadmap

- [x] 1. Operator keys server-side, hidden from users
- [x] 2. Connect Wallet (Phantom)
- [x] 3. Multi-user (session per pubkey)
- [x] 4. Settings panel + strategy picker
- [x] 5. Non-custodial execution (Jupiter + Phantom signing) + wallet sign-in + Twitter/KOL
- [ ] 6. Bot modes: N1 (click-to-confirm) / N2 (semi-auto)
- [ ] 7. N3 autopilot via session keys; hosting; DB; monetization

## Disclaimer

Experimental research software. Memecoins are an extremely high-risk asset — you can lose everything. Nothing here is investment advice. Use at your own risk.
