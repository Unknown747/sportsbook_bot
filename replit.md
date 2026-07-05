# Sportsbook Auto Betting Agent

A modular Python-based auto-betting bot that integrates with the Stake.com GraphQL API using IDR (Indonesian Rupiah) currency.

## Project Structure

All modules live flat at the project root (no subpackage) to avoid a folder/script naming clash:

```
main.py                 # Root entry point
bot_main.py             # Core orchestrator with 10-minute loop
bot_config.py           # API config, IDR limits, risk management constants
predictor.py            # Ensemble ML + Multi-Agent LLM consensus predictions
arbitrage_finder.py     # 3-way match scanner for value bets & arbitrage
bet_sizer.py            # Fractional Kelly Criterion calculator with drawdown protection
executor.py             # Stake GraphQL API client & bet history logger
fetcher.py              # OddsAPI / Stake data fetching
telegram_notifier.py    # Telegram alert sender
requirements.txt        # Python dependencies
```

## Running

```bash
python main.py
```

## Configuration

Edit `bot_config.py` to set:
- `STAKE_SESSION_TOKEN` — your Stake.com x-access-token cookie
- `SIMULATION_MODE` — set to `False` for live betting
- `INITIAL_BANKROLL` — starting balance in IDR

## User Preferences

- Use PEP 8 style and type hints throughout
- Keep modules small and single-responsibility
