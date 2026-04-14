# OGfinder Telegram Bot

Telegram bot that finds the original (OG) Solana token by name, scans a mint address to check if it's the OG, or searches for tokens linked to a social URL.

## Setup

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure environment:**
   ```bash
   cp .env.example .env
   ```
   Fill in your keys:
   - `TELEGRAM_BOT_TOKEN` — Get from [@BotFather](https://t.me/BotFather) (required)
   - `HELIUS_API_KEY` — Get from [helius.dev](https://helius.dev) (required for full functionality)
   - `BIRDEYE_API_KEY` — Get from [birdeye.so](https://birdeye.so) (optional, improves social link search)
   - `SOLANA_RPC_URL` — Custom RPC endpoint (optional, falls back to public mainnet)

3. **Run:**
   ```bash
   python bot.py
   ```

## Usage

Send messages to the bot on Telegram:

| Input | Mode | Example |
|-------|------|---------|
| Token name | OG search | `pepe` |
| Mint address | Mint scan | `So1...4xkR` |
| Social URL | Link search | `https://x.com/someproject` |

Or use explicit commands:
- `/og <name>` — Search by token name
- `/scan <mint>` — Scan a mint address
- `/link <url>` — Search by social link
- `/start` — Show help

## How it works

The bot queries DexScreener, Jupiter, Helius, and Birdeye APIs to find all tokens matching your query, then ranks them by creation time to determine which is the original (OG).

A background poller continuously indexes DexScreener token profiles and Birdeye new listings into a local SQLite database for faster social link lookups.
# og-bot
