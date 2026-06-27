# GMGN AI Trader (local)

English · **[中文](README.md)**

Dashboard screens, the human trades: deterministic rules cast a wide net → scoring cuts hard → the LLM only explains the survivors → you press one-click buy.
A separate position escape monitor flags rug signals and prompts a one-click exit. The LLM never touches risk control or the escape path.

> Full design / API contract / current progress live in [SPEC.en.md](SPEC.en.md). This file only covers how to run it.

## Directory layout
```
aitrader/
├── app.py                 FastAPI backend + screening pipeline (self-contained)
├── requirements.txt       fastapi, uvicorn
├── static/
│   └── index.html         frontend dashboard (source file — edit this for local dev; backend serves it same-origin)
├── docs/
│   └── index.html         copy of static/index.html, for GitHub Pages publishing
├── outputs/
│   ├── trade_decisions.jsonl   generated at runtime (SCREEN/FILTER/BUY/SELL/UNMONITOR log)
│   ├── positions.json          generated at runtime (positions persisted to disk, survive restart)
│   └── trending_cmds.json      generated at runtime (per-chain trending command overrides, survive restart; the gear's "↺ Reset" deletes back to default)
├── README.md
└── SPEC.md
```
Credentials are not in the project; they are written to your machine at runtime: `~/.config/gmgn/.env` (chmod 600).

## Setup (one-time)
1. Python 3.10+
2. Only needed for LIVE / real market data: install `gmgn-cli` (verified against **1.3.9**; the 1.0.x interface is no longer compatible)
   ```bash
   npm install -g gmgn-cli@1.3.9
   ```
3. Only needed for LIVE / real market data: go to gmgn.ai/ai and apply for an API Key using your own Ed25519 public key + egress IP
   (the key is bound to the IP whitelist you applied with — each user uses their own, they cannot be shared)

You can run without gmgn-cli and without a key — by default it uses the built-in `MockGMGN` adapter.

4. Only needed if you edit the frontend: enable the git hook (auto-syncs `static/index.html` to `docs/` on commit)
   ```bash
   git config core.hooksPath scripts/git-hooks
   ```
   The hook script ships with the repo, but this `git config` line is local config and does not travel with a clone, so **each person must run it once after cloning**. If you don't, editing `static/` won't update `docs/`, and the GitHub Pages demo will silently stay on the old version.

## Run
```bash
# from the skillmarket-demos repo root
cd aitrader
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 127.0.0.1 --port 8000
```
Open http://127.0.0.1:8000 in your browser.

> On startup the backend reads the key from `~/.config/gmgn/.env`; if a key is present it automatically switches to real data — no need to enter it in the frontend.

## Usage
- Default Mock adapter + SHADOW mode: **runs without a key** (for integration testing).
- With a key configured it connects to real market data automatically; the top-right gear lets you edit the trending command / poll interval per chain.
- Top-right CHAIN dropdown switches **SOL / BSC / Base / ETH** (each tab keeps its own chain via sessionStorage, so multiple tabs don't interfere); the top-right **MODE icon toggles LIVE/SHADOW on click**.
- Candidates that pass all gates land in "awaiting your decision"; a trade happens only when you click "one-click buy".
- Positions appear in the escape monitor on the right; once they degrade past the threshold (severity ≥ 70) an "exit now" prompt pops up.

## LIVE / SHADOW (important)
- **SHADOW (paper trading) = the default safe state**: buy/sell only writes to the log + positions.json, and **sends no on-chain transactions**.
- **LIVE = real trades, real funds, irreversible**: requires ① clicking the **MODE icon top-right to switch to LIVE** (with a second confirmation) + ② `GMGN_PRIVATE_KEY` configured in `~/.config/gmgn/.env` (an Ed25519 PEM signing key, not a wallet private key).
- `LIVE_TRADING_DISABLED` at the top of `app.py`: **currently `False` (unlocked)**. Set it back to `True` to instantly seal off all on-chain writes (even if switched to LIVE it forces SHADOW and never calls `swap()`).
- It's still **human-in-the-loop**: a trade happens only when you click "one-click buy/sell"; after a backend restart the mode reverts to SHADOW (LIVE is not persisted) and must be switched again.
- A buy **polls to confirm the real fill**: on failure it records no position and does not lie; the fill prompt includes the tx hash — checking it on a block explorer yourself is the most reliable.
- ⚠️ Real trading is **currently only fully verified on Solana**; for EVM see "Known limitations" below.

## GitHub Pages demo
When not on localhost (e.g. github.io) and the backend is unreachable, the frontend **automatically enters DEMO mode**: it shows sample data, displays a demo banner, sends no fetches, and cannot place orders (purely static, zero API, zero key).
Deploy: Settings → Pages → `main` branch `/docs`. After editing the frontend, the pre-commit hook auto-syncs `static/index.html` to `docs/`.

## Security
- The backend binds only to 127.0.0.1 — never change it to 0.0.0.0 or expose it to the public internet.
- Keys are sent only to the local backend, written to a local .env, and never stored in the browser.

## Known limitations / TODO

**⚠ Auto sell strategy at buy time — not yet implemented**
The "exit plan (hard stop-loss / TP ladder / trailing stop)" shown in the frontend buy dialog is **display text only** for now (`exit_plan()`). The `swap()` that `do_buy` calls for the real order **does not pass `--condition-orders`, so no take-profit/stop-loss orders are actually placed**. After buying you can only watch manually + rely on the escape monitor + exit by hand. TODO: assemble `--condition-orders` from the TP/SL ladder and submit it together with the swap (parameter semantics / per-chain support need to be verified against the real interface).

**⚠ EVM chain screening looks buggy — only Solana is fully working today**
- **Solana**: the full path — screen / buy / sell / position monitoring — is verified (including a real signed `order quote`).
- **EVM (BSC / Base / ETH)**: the plumbing is wired (adapter / native token `0x0` / 18-decimal precision / wallet resolution / bsc's default fourmeme platform command all aligned to the authoritative table), but **screening results look wrong/incomplete and are not fully verified, and buy/sell has not been live-tested per chain**. Suspected issues: whether EVM `market trending` row fields match the Solana assumptions in `FeatureExtractor.build_from_row` / `hard_gates` (`is_honeypot`/`renounced_mint`/`bundler_rate`/`buys/sells` etc. may be named differently or be missing → mis-firing the rug gate / momentum scoring); whether base/eth need a per-chain launchpad platform. **For now EVM is recommended for read-only browsing only; before going live, investigate and verify with small amounts chain by chain.**

**Pending integration (marked in code, see SPEC §11)**
- `LLMJudge.judge`: currently a momentum-heuristic placeholder; in production swap in a real LLM (fed the sanitized `symbol_safe` + numeric features, never the raw token name).
- `priority_score`: currently a deterministic momentum weighting (the doc's "ML ranking"); can be swapped for a lightweight model.
- Feedback flywheel: trade_decisions.jsonl is already the raw material; once backfilled with realized PnL it can tune the CFG thresholds (currently write-only).
