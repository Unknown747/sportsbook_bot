"""
Executor module for Sportsbook Auto Betting Agent.
Handles bet placement via Stake.com GraphQL API and maintains bet history log.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

from sportsbook_bot import config

logger = logging.getLogger(__name__)

BET_HISTORY_FILE: str = "bet_history.json"

CREATE_SPORTS_BET_MUTATION = """
mutation CreateSportsBet($input: SportsBetInput!) {
  createSportsBet(input: $input) {
    id
    status
    amount
    currency
    createdAt
  }
}
"""


class StakeExecutor:
    """Handles Stake.com bet placement and history tracking."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "x-api-key": config.STAKE_API_KEY,
            }
        )
        self._ensure_history_file()

    def _ensure_history_file(self) -> None:
        """Initialize bet history file if it does not exist."""
        if not os.path.exists(BET_HISTORY_FILE):
            with open(BET_HISTORY_FILE, "w", encoding="utf-8") as f:
                json.dump([], f)

    def _load_history(self) -> list:
        """Load existing bet history from file."""
        try:
            with open(BET_HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return []

    def _save_history(self, history: list) -> None:
        """Persist bet history to file."""
        with open(BET_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2, ensure_ascii=False)

    def _log_bet(self, record: dict) -> None:
        """Append a bet record to bet_history.json."""
        history = self._load_history()
        history.append(record)
        self._save_history(history)

    def place_stake_bet(
        self,
        active_odds_id: str,
        target_stake: float,
        match_info: Optional[dict] = None,
    ) -> dict:
        """
        Place a sports bet on Stake.com via GraphQL mutation.

        In SIMULATION_MODE the bet is logged without hitting the API.

        Args:
            active_odds_id: The Stake odds/market ID to bet on.
            target_stake: Amount in IDR to wager.
            match_info: Optional metadata about the match for history logging.

        Returns:
            Response dict with keys: success (bool), data (dict), error (str|None).
        """
        timestamp = datetime.now(timezone.utc).isoformat()

        base_record = {
            "timestamp": timestamp,
            "active_odds_id": active_odds_id,
            "stake_idr": target_stake,
            "currency": config.STAKE_CURRENCY,
            "simulation": config.SIMULATION_MODE,
            "match_info": match_info or {},
        }

        if config.SIMULATION_MODE:
            simulated_result = {
                "id": f"sim_{active_odds_id}_{timestamp}",
                "status": "SIMULATED",
                "amount": target_stake,
                "currency": config.STAKE_CURRENCY,
                "createdAt": timestamp,
            }
            record = {**base_record, "status": "SIMULATED", "response": simulated_result}
            self._log_bet(record)
            logger.info("[SIMULATION] Bet logged: %s @ %.2f IDR", active_odds_id, target_stake)
            return {"success": True, "data": simulated_result, "error": None}

        payload = {
            "query": CREATE_SPORTS_BET_MUTATION,
            "variables": {
                "input": {
                    "activeOddsId": active_odds_id,
                    "amount": target_stake,
                    "currency": config.STAKE_CURRENCY,
                }
            },
        }

        try:
            response = self.session.post(
                config.STAKE_API_URL, json=payload, timeout=15
            )
            response.raise_for_status()
            data = response.json()

            if "errors" in data:
                error_msg = str(data["errors"])
                record = {**base_record, "status": "API_ERROR", "error": error_msg}
                self._log_bet(record)
                logger.error("GraphQL error placing bet: %s", error_msg)
                return {"success": False, "data": {}, "error": error_msg}

            bet_data = data.get("data", {}).get("createSportsBet", {})
            record = {**base_record, "status": bet_data.get("status", "UNKNOWN"), "response": bet_data}
            self._log_bet(record)
            logger.info("Bet placed: %s status=%s", active_odds_id, bet_data.get("status"))
            return {"success": True, "data": bet_data, "error": None}

        except requests.exceptions.RequestException as exc:
            error_msg = str(exc)
            record = {**base_record, "status": "REQUEST_ERROR", "error": error_msg}
            self._log_bet(record)
            logger.error("Request error placing bet: %s", error_msg)
            return {"success": False, "data": {}, "error": error_msg}
