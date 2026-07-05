---
name: Stake sportsbook API limitation
description: Stake.com GraphQL API does not expose live sportsbook odds/markets via x-access-token; only casino + account queries work.
---

## Rule
Do NOT attempt to fetch live odds or place bets via Stake GraphQL API using x-access-token. The API returns 400 or empty data for sportEvents/sportFixtures. Only the-odds-api.com (ODDS_API_KEY) provides real match data.

**Why:** Stake.com restricts sportsbook market/odds access to its own frontend (Cloudflare-protected). The API key (x-access-token) only grants access to casino games, user account, and tournament lists.

**How to apply:** Any feature requiring live odds must go through OddsAPI. Bet placement must be manual (user places on Stake website). The bot is a signal-only system.

## What works via Stake API
- `user { id name balances }` — auth verification
- `sportList` — list of sports
- `tournamentList` — active tournaments
- `allSportBets` — betting history
- `createSportsBet` mutation — placing bets (if odds ID available, which it isn't)
