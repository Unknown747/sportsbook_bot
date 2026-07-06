---
name: OddsAPI key validation
description: How to validate a the-odds-api.com API key without burning request quota, and how to interpret response codes.
---

To check whether an ODDS_API_KEY (the-odds-api.com) is valid, call `GET https://api.the-odds-api.com/v4/sports/?apiKey=<key>`. This endpoint does not consume the monthly request quota, making it safe to use for setup-time validation.

**Response codes:**
- `200` — key is valid and active.
- `401` — key is invalid, malformed, or not activated.
- `429` — key is valid but the monthly quota is exhausted (still treat as "valid" for setup purposes; the user just needs to wait for reset or use a different key).

**Why:** Running the full bot to discover an invalid key wastes a cycle and produces confusing downstream errors (e.g. "all key slots exhausted"). Validating up front during setup gives immediate, clear feedback.

**How to apply:** Use this endpoint in any setup/onboarding script (e.g. `setup.sh`) right after the user provides their ODDS_API_KEY, before proceeding to install dependencies or run the bot.
