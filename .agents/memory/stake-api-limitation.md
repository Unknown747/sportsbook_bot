---
name: Stake sportsbook API limitation
description: Stake API key only works for casino and account queries; sportsbook odds are blocked
---

## Rule
Stake.com `x-access-token` API key CANNOT fetch sportsbook event data (sportEvents, sportFixtures, sportMarket).
All sportsbook odds queries return HTTP 400 or empty results.

**Why:** Stake's policy restricts sportsbook data access via API key — only casino game data and user account queries succeed.

**How to apply:** 
- Use OddsAPI (the-odds-api.com) as primary odds source
- `active_odds_ids` in match data are PLACEHOLDER strings (OddsAPI event ID + "_home/away/draw"), NOT real Stake market IDs
- For real live betting on Stake sports, user must supply a valid `activeOddsId` from Stake's internal system (obtainable via browser network inspector on stake.com)
- Working queries: `user`, `sportList`, `tournamentList`, `allSportBets`
- GraphQL endpoint: `https://stake.com/_api/graphql` with header `x-access-token`
