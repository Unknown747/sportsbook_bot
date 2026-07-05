"""
Fetcher module for Sportsbook Auto Betting Agent.
Queries Stake.com GraphQL API for live and upcoming sports events with odds.
Falls back to sample data if the API is unreachable or returns no results.
"""

import logging
from typing import Optional

import requests

from sportsbook_bot import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# GraphQL Queries
# ---------------------------------------------------------------------------

QUERY_SPORT_EVENTS = """
query GetSportEvents($sport: String, $limit: Int, $status: SportEventStatus) {
  sportEvents(sport: $sport, limit: $limit, status: $status) {
    id
    slug
    name
    status
    startTime
    home {
      id
      name
    }
    away {
      id
      name
    }
    markets {
      id
      slug
      name
      status
      outcomes {
        id
        slug
        name
        odds
        status
      }
    }
  }
}
"""

QUERY_SPORT_FIXTURES = """
query GetFixtures($sport: String, $limit: Int) {
  fixtures(sport: $sport, limit: $limit) {
    id
    name
    startTime
    homeTeam { id name }
    awayTeam { id name }
    odds {
      home
      draw
      away
      homeOddsId
      drawOddsId
      awayOddsId
    }
  }
}
"""


class StakeFetcher:
    """Fetches live and upcoming sport events from Stake.com GraphQL API."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Content-Type": "application/json",
                "x-api-key": config.STAKE_API_KEY,
            }
        )

    def _post(self, query: str, variables: dict) -> Optional[dict]:
        """
        Send a GraphQL POST request to Stake.com.

        Args:
            query: GraphQL query string.
            variables: Variables dict for the query.

        Returns:
            Parsed JSON response dict, or None on failure.
        """
        payload = {"query": query, "variables": variables}
        try:
            resp = self.session.post(config.STAKE_API_URL, json=payload, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            if "errors" in data:
                logger.warning("GraphQL errors: %s", data["errors"])
                return None
            return data.get("data")
        except requests.exceptions.Timeout:
            logger.error("Stake API timeout saat fetch events.")
        except requests.exceptions.ConnectionError:
            logger.error("Tidak dapat terhubung ke Stake API.")
        except requests.exceptions.HTTPError as exc:
            logger.error("HTTP error dari Stake API: %s", exc)
        except Exception as exc:
            logger.error("Unexpected error fetching events: %s", exc)
        return None

    def _parse_sport_events(self, data: dict) -> list:
        """
        Parse sportEvents response into bot match format.

        Args:
            data: Raw 'data' field from GraphQL response.

        Returns:
            List of match dicts compatible with the bot's pipeline.
        """
        events = data.get("sportEvents") or []
        matches = []

        for event in events:
            home = event.get("home") or {}
            away = event.get("away") or {}
            home_name = home.get("name", "Home Team")
            away_name = away.get("name", "Away Team")
            event_id = event.get("id", "unknown")

            # Extract 1X2 market (home/draw/away)
            odds_data: dict = {}
            active_odds_ids: dict = {}

            for market in event.get("markets") or []:
                market_slug = (market.get("slug") or market.get("name") or "").lower()
                # Look for match result / 1x2 / full-time market
                if not any(kw in market_slug for kw in ("1x2", "match_result", "result", "winner", "full")):
                    continue
                if market.get("status") not in (None, "ACTIVE", "active", "OPEN", "open"):
                    continue

                stake_odds: dict = {}
                for outcome in market.get("outcomes") or []:
                    if outcome.get("status") not in (None, "ACTIVE", "active", "OPEN", "open"):
                        continue
                    name = (outcome.get("name") or "").lower()
                    odds_val = outcome.get("odds")
                    outcome_id = outcome.get("id")

                    if odds_val is None or outcome_id is None:
                        continue

                    try:
                        odds_float = float(odds_val)
                    except (TypeError, ValueError):
                        continue

                    if "home" in name or name == "1":
                        stake_odds["Home"] = odds_float
                        active_odds_ids["Home"] = outcome_id
                    elif "draw" in name or name == "x":
                        stake_odds["Draw"] = odds_float
                        active_odds_ids["Draw"] = outcome_id
                    elif "away" in name or name == "2":
                        stake_odds["Away"] = odds_float
                        active_odds_ids["Away"] = outcome_id

                if len(stake_odds) == 3:
                    odds_data["stake"] = stake_odds
                    break  # Found a complete 1X2 market

            if len(odds_data.get("stake", {})) < 3:
                logger.debug("Event %s tidak punya odds 1X2 lengkap, dilewati.", event_id)
                continue

            matches.append(
                {
                    "match_id": event_id,
                    "home_team": home_name,
                    "away_team": away_name,
                    "home_strength": 50.0,
                    "away_strength": 50.0,
                    "league_draw_rate": 0.27,
                    "sentiment_home_bias": 0.0,
                    "odds_data": odds_data,
                    "active_odds_ids": active_odds_ids,
                }
            )

        return matches

    def _parse_fixtures(self, data: dict) -> list:
        """
        Parse fixtures response into bot match format.

        Args:
            data: Raw 'data' field from GraphQL response.

        Returns:
            List of match dicts compatible with the bot's pipeline.
        """
        fixtures = data.get("fixtures") or []
        matches = []

        for fix in fixtures:
            odds_block = fix.get("odds") or {}
            home_odds = odds_block.get("home")
            draw_odds = odds_block.get("draw")
            away_odds = odds_block.get("away")

            if not all([home_odds, draw_odds, away_odds]):
                continue

            try:
                stake_odds = {
                    "Home": float(home_odds),
                    "Draw": float(draw_odds),
                    "Away": float(away_odds),
                }
            except (TypeError, ValueError):
                continue

            matches.append(
                {
                    "match_id": fix.get("id", "unknown"),
                    "home_team": (fix.get("homeTeam") or {}).get("name", "Home"),
                    "away_team": (fix.get("awayTeam") or {}).get("name", "Away"),
                    "home_strength": 50.0,
                    "away_strength": 50.0,
                    "league_draw_rate": 0.27,
                    "sentiment_home_bias": 0.0,
                    "odds_data": {"stake": stake_odds},
                    "active_odds_ids": {
                        "Home": odds_block.get("homeOddsId", ""),
                        "Draw": odds_block.get("drawOddsId", ""),
                        "Away": odds_block.get("awayOddsId", ""),
                    },
                }
            )

        return matches

    def fetch_live_matches(self, sport: str = "soccer", limit: int = 20) -> list:
        """
        Fetch live and upcoming matches from Stake.com.

        Tries two query strategies in sequence:
          1. sportEvents query (primary)
          2. fixtures query (fallback)

        Args:
            sport: Sport slug e.g. "soccer", "basketball", "tennis".
            limit: Maximum number of events to fetch per request.

        Returns:
            List of match dicts, or empty list if API is unreachable.
        """
        logger.info("Fetching live matches dari Stake API (sport=%s, limit=%d)...", sport, limit)

        # Strategy 1: sportEvents
        data = self._post(
            QUERY_SPORT_EVENTS,
            {"sport": sport, "limit": limit, "status": "LIVE"},
        )
        if data:
            matches = self._parse_sport_events(data)
            if matches:
                logger.info("sportEvents: %d pertandingan LIVE ditemukan.", len(matches))
                return matches

        # Strategy 1b: upcoming events
        data = self._post(
            QUERY_SPORT_EVENTS,
            {"sport": sport, "limit": limit, "status": "UPCOMING"},
        )
        if data:
            matches = self._parse_sport_events(data)
            if matches:
                logger.info("sportEvents: %d pertandingan UPCOMING ditemukan.", len(matches))
                return matches

        # Strategy 2: fixtures
        data = self._post(QUERY_SPORT_FIXTURES, {"sport": sport, "limit": limit})
        if data:
            matches = self._parse_fixtures(data)
            if matches:
                logger.info("fixtures: %d pertandingan ditemukan.", len(matches))
                return matches

        logger.warning("Tidak ada data dari Stake API untuk sport '%s'.", sport)
        return []
