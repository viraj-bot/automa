# Automa — Telegram Trade Signal Executor

Automa listens to a Telegram group for trade signals (options entries, exits, book-profit calls), parses them into structured orders, and executes them via the **Groww Trading API**. It also supports **paper trading** and **backtesting** against historical data.

---

## Features

- **Real-time signal detection** — Listens to your Telegram group 24/7 using Telethon
- **Smart message parsing** — Regex-based parser handles entry, exit, and book-profit signals with flexible formatting
- **Groww API integration** — Places real orders via the official `growwapi` Python SDK
- **Paper trading mode** — Simulate trades without risking real money
- **Backtesting** — Replay historical Telegram messages with actual market prices from Groww's historical candle API
- **Risk management** — Configurable max risk per trade, automatic stop-loss placement
- **Idempotent processing** — Duplicate signals are detected and skipped
- **Cloud-ready** — Docker + docker-compose for easy deployment anywhere

---

## Prerequisites

- Python 3.11+
- A Telegram account (not a bot — the app uses the user API via Telethon)
- A Groww account with an active Trading API subscription
- Telegram API credentials from [my.telegram.org](https://my.telegram.org)
- Groww API token from [groww.in/trade-api/api-keys](https://groww.in/trade-api/api-keys)

---

## Quick Start

### 1. Clone and install

```bash
git clone <your-repo-url> automa
cd automa
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

| Variable | Description |
|---|---|
| `TELEGRAM_API_ID` | From [my.telegram.org](https://my.telegram.org) |
| `TELEGRAM_API_HASH` | From [my.telegram.org](https://my.telegram.org) |
| `TELEGRAM_GROUP_ID` | Numeric chat ID of your signal group |
| `GROWW_API_TOKEN` | From [Groww API Keys](https://groww.in/trade-api/api-keys) |
| `MODE` | `live`, `paper`, or `backtest` |
| `DEFAULT_LOT_MULTIPLIER` | Number of lots per signal (default: 1) |
| `MAX_RISK_PER_TRADE` | Max risk in INR per trade (default: 5000) |
| `DEFAULT_PRODUCT` | `NRML` (overnight) or `MIS` (intraday) |

**Finding your Telegram Group ID:**
Forward any message from the group to [@userinfobot](https://t.me/userinfobot) on Telegram, or use the Telethon snippet:

```python
from telethon import TelegramClient
client = TelegramClient('session', api_id, api_hash)
client.start()
for dialog in client.iter_dialogs():
    print(dialog.name, dialog.id)
```

### 3. First run (Telegram login)

On the first run, Telethon will ask you to log in with your phone number and a verification code:

```bash
python main.py --mode paper
```

The session is saved to `data/automa_session.session` and reused on subsequent runs.

### 4. Run in different modes

```bash
# Paper trading (default) — simulates trades, no real money
python main.py --mode paper

# Live trading — places real orders on Groww
python main.py --mode live

# Backtest — replay last 30 days of group history
python main.py --mode backtest --days 30

# Backtest with message limit
python main.py --mode backtest --days 60 --limit 500
```

---

## Supported Signal Formats

The parser handles these message patterns (and variations):

### Entry
```
New option Trade
Buy NIFTY50 17 FEB 25600 PE at ₹135
target 1: 185
target 2: 235
Stoploss: 90
Act Now
```

### Exit
```
Exit Nifty50 17 FEB 25600 PE
Please exit from NIFTY50 17 FEB
```

### Book Profit
```
Book Profit in INDHOTEL 24 FEB 690 CE at price 17.2
```

The parser is lenient with formatting — it strips emojis, normalises whitespace, and handles case-insensitive matching.

---

## Project Structure

```
automa/
├── main.py                 # CLI entry point
├── config/
│   └── settings.py         # Pydantic settings from .env
├── telegram/
│   ├── listener.py         # Real-time Telethon message handler
│   └── history.py          # Fetch chat history for backtesting
├── parser/
│   ├── models.py           # TradeSignal dataclasses
│   └── signal_parser.py    # Regex-based message parser
├── broker/
│   ├── base.py             # Abstract broker interface
│   ├── groww_broker.py     # Groww API live execution
│   └── paper_broker.py     # Paper trading simulator
├── backtest/
│   ├── engine.py           # Backtest engine
│   └── report.py           # Rich-formatted report output
├── storage/
│   ├── db.py               # Async SQLite database layer
│   └── models.py           # SQL schema definitions
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example
└── README.md
```

---

## Docker Deployment

### Build and run

```bash
docker compose up -d
```

### Run in a specific mode

```bash
# Live mode
docker compose run automa python main.py --mode live

# Backtest
docker compose run automa python main.py --mode backtest --days 60
```

### Deploy to cloud

The Docker image works on any platform that supports containers:

- **Railway**: `railway up`
- **Render**: Connect your repo, set the Docker build path
- **AWS ECS / Fargate**: Push to ECR, create a task definition
- **DigitalOcean App Platform**: Connect repo, select Dockerfile

Set environment variables in your cloud platform's dashboard instead of using a `.env` file.

---

## Architecture

```
Telegram Group
    │
    ▼
Telethon Listener (real-time)
    │
    ▼
Signal Parser (regex)
    │
    ├── ENTRY  ──► Broker.execute_entry()
    ├── EXIT   ──► Broker.execute_exit()
    └── BOOK_PROFIT ──► Broker.execute_book_profit()
                            │
                ┌───────────┴───────────┐
                ▼                       ▼
        GrowwBroker (live)      PaperBroker (paper)
                │                       │
                └───────────┬───────────┘
                            ▼
                    SQLite Trade Log
```

---

## Important Notes

- **Groww API token expires daily** at 6:00 AM IST. You need to either regenerate it daily or use the API key/secret flow with daily approval on the Groww dashboard.
- **Telethon uses your personal Telegram account**, not a bot. This means it can read any group you're a member of.
- **Risk management**: Always start with paper mode to validate that signals are being parsed correctly before switching to live.
- **The parser may not catch every signal format**. Check the logs for unparsed messages and adjust the regex patterns in `parser/signal_parser.py` as needed.

---

## License

Private / Internal use.
