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
- Confirmed (2026-07-05) with a live valid API key: `sportList` works, but fixture/market discovery fields do not exist as the bot code assumes (`sportMatchGroup` doesn't exist; `sportCategory` requires an internal `categoryId`, not a slug) — there is no straightforward API-key-only path to enumerate live matches and get real bettable market IDs
- Conclusion: full auto-bet execution (fetch odds → auto place on Stake) is NOT achievable via API key alone; recommended pattern is bot generates signals (Telegram alert) and user places the bet manually on stake.com
- GraphQL endpoint: `https://stake.com/_api/graphql` with header `x-access-token`
