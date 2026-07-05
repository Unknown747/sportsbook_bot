"""
Predictor module for Sportsbook Auto Betting Agent.
Provides ensemble ML simulation and multi-agent LLM consensus for 3-way match predictions.
"""

import random
from typing import Dict


def _normalize(probs: Dict[str, float]) -> Dict[str, float]:
    """Normalize probability dict so values sum to 1.0."""
    total = sum(probs.values())
    if total == 0:
        return {"Home": 1 / 3, "Draw": 1 / 3, "Away": 1 / 3}
    return {k: v / total for k, v in probs.items()}


def get_ensemble_prediction(match_data: dict) -> Dict[str, float]:
    """
    Simulate an ensemble ML model prediction for a 3-way match outcome.

    Args:
        match_data: Dictionary containing match metadata (teams, league, form, etc.)

    Returns:
        Normalized probability dict: {"Home": float, "Draw": float, "Away": float}
    """
    home_strength = match_data.get("home_strength", 50.0)
    away_strength = match_data.get("away_strength", 50.0)
    league_factor = match_data.get("league_draw_rate", 0.25)

    home_advantage = 0.05
    base_home = home_strength / (home_strength + away_strength) + home_advantage
    base_away = away_strength / (home_strength + away_strength)

    noise = random.uniform(-0.03, 0.03)
    raw = {
        "Home": max(0.0, base_home + noise),
        "Draw": max(0.0, league_factor + random.uniform(-0.02, 0.02)),
        "Away": max(0.0, base_away - noise),
    }
    return _normalize(raw)


def get_multi_agent_consensus(match_data: dict) -> Dict[str, float]:
    """
    Simulate a multi-agent LLM consensus among three analytical agents.

    Agents:
      - Statistician: Focuses on historical stats and form.
      - Tactician: Evaluates tactical matchups.
      - Sentiment Analyst: Evaluates news, injuries, morale signals.

    Args:
        match_data: Dictionary containing match metadata.

    Returns:
        Normalized probability dict: {"Home": float, "Draw": float, "Away": float}
    """
    home_strength = match_data.get("home_strength", 50.0)
    away_strength = match_data.get("away_strength", 50.0)
    home_advantage = 0.05

    base_home = home_strength / (home_strength + away_strength)
    base_away = away_strength / (home_strength + away_strength)
    base_draw = match_data.get("league_draw_rate", 0.25)

    statistician: Dict[str, float] = {
        "Home": base_home + home_advantage + random.uniform(-0.02, 0.02),
        "Draw": base_draw + random.uniform(-0.01, 0.01),
        "Away": base_away + random.uniform(-0.02, 0.02),
    }

    tactician: Dict[str, float] = {
        "Home": base_home + home_advantage * 1.2 + random.uniform(-0.03, 0.03),
        "Draw": base_draw * 0.9 + random.uniform(-0.01, 0.02),
        "Away": base_away * 0.95 + random.uniform(-0.02, 0.03),
    }

    sentiment_bias = match_data.get("sentiment_home_bias", 0.0)
    sentiment: Dict[str, float] = {
        "Home": base_home + sentiment_bias + random.uniform(-0.02, 0.02),
        "Draw": base_draw + random.uniform(-0.02, 0.02),
        "Away": base_away - sentiment_bias * 0.5 + random.uniform(-0.02, 0.02),
    }

    agents = [statistician, tactician, sentiment]
    consensus: Dict[str, float] = {}
    for outcome in ("Home", "Draw", "Away"):
        values = [max(0.0, agent[outcome]) for agent in agents]
        consensus[outcome] = sum(values) / len(values)

    return _normalize(consensus)
