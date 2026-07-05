"""
Configuration module for Sportsbook Auto Betting Agent.
Contains all constants, API settings, and risk management parameters.
"""

INITIAL_BANKROLL: float = 100000.0
KELLY_MULTIPLIER: float = 0.10
MIN_VALUE_EDGE: float = 0.05
MIN_BET_IDR: float = 2000.0
MAX_BET_IDR: float = 5000.0
MAX_DAILY_DRAWDOWN: float = 0.10

STAKE_API_URL: str = "https://api.stake.com/graphql"
STAKE_API_KEY: str = __import__("os").environ.get("STAKE_API_KEY", "")
STAKE_CURRENCY: str = "idr"

SIMULATION_MODE: bool = True

LOOP_INTERVAL_SECONDS: int = 600
