# Strategy draft: Social-momentum options ("WSB/X attention")

| | |
|---|---|
| Status | **DRAFT v0.4** — owner decisions of 2026-10-18 incorporated; not yet validated on paper, not for live trading |
| Owner | Sergej Samokhin |
| Date | 2026-10-18 |
| Scope | One upstream producer (`social-momentum`) that emits signals under Signal Contract 1.0 |
| Capital | **Monthly loss limit €100** (net, per calendar month). Data and tool costs are budgeted separately (section 8). |
| Changes v0.2 | €100 confirmed as monthly loss limit; data costs on top; data sourcing reworked (Apify / Firecrawl / X API compared, section 3.1 and 8) |
| Changes v0.3 | Scraping terms-of-service risk accepted; running costs capped at USD 50/month (section 8 reworked); margin account confirmed; monthly loss limit now enforced by the risk engine (`MAX_MONTHLY_LOSS_USD`) |
| Changes v0.4 | Weekly entry cap in the risk engine (`MAX_ENTRIES_PER_WEEK`); time stop as signal field `exit.max_holding_days`; Setup B kept on paper by `paper_only_strategies` in the producer registry; earnings/ex-dividend filter stays with the producer (owner decision) |

> This is a strategy design, not investment advice. Options can lose 100 % of the premium paid.
> Social-media-driven stocks are among the most volatile and most manipulated instruments in the
> market. Every number below is a proposed starting value to be validated on the paper account.

---

## 0. Summary in ten lines

1. **Signal:** unusual, *accelerating* attention on a ticker in r/wallstreetbets and on X, plus
   sentiment direction, **confirmed by price and volume** before any order is placed.
2. **Two setups:** *Early momentum* (bullish, ride the move while attention is rising) and
   *Exhaustion fade* (bearish, after attention and price have overshot and price starts to roll over).
3. **Instruments:** **debit vertical spreads only** (`bull_call_spread`, `bear_put_spread`).
   No naked short options (unlimited/large risk, not supported by the system anyway). No single
   long options for this strategy (hype names carry inflated implied volatility; spreads neutralise
   much of that).
4. **Contract:** 21–45 days to expiry, long leg ~0.50 delta, short leg ~0.25 delta.
5. **Size:** max **USD 40** debit per position (= max loss), **1 contract**, max **2** open positions.
6. **Exits:** take profit **+75 %** of debit, stop loss **−50 %**, close **7 days** before expiry,
   time stop after 10 trading days (`exit.max_holding_days`), thesis exits (attention collapse,
   sentiment flip).
7. **Monthly loss limit €100 (≈ USD 115):** when the month's net realised P&L minus the max loss
   of open positions reaches −USD 115, no new entries until the next month. The month can
   therefore never lose more than €100 from trading. Enforced by the risk engine
   (`MAX_MONTHLY_LOSS_USD=115`, rule `monthly_loss_limit`).
8. **Expected trade count:** 3–6 per month. Break-even hit rate incl. costs ≈ **45 %**.
9. **Running costs on top of the loss limit, capped at USD 50/month:** ≈ USD 29 for social data
   (Apify Starter) plus LLM, IBKR market data and hosting within fixed sub-caps (section 8), via a two-stage pipeline (free Reddit/aggregator
   counts → paid X look-ups via Apify for ~10 candidate tickers per day). The official X API (pay-per-use) would cost ~USD 200+/month for the same
   volume; Firecrawl is not suited for Reddit/X (section 3.1).
10. **Go-live only after** ≥ 30 paper signals over ≥ 8 weeks meet the gates in section 9.

---

## 1. Thesis and why it might (not) work

**Thesis.** Retail attention moves prices of mid/small caps in the short term. When attention
accelerates *from a low base* and price/volume confirm, there is a short continuation window
(days to a few weeks). When attention is already extreme, the subsequent returns tend to be
weaker and mean-reverting.

**What the evidence suggests (qualitative, to be validated by our own paper results):**

- Academic work on WallStreetBets (e.g. Bradley, Hanousek, Jame & Xiao, *Place Your Bets?*)
  found some predictive value in early "due diligence" posts, which weakened after the 2021 GameStop
  episode once the forum became mainstream.
- Research on attention-driven retail herding (e.g. Barber, Huang, Odean & Schwarz on Robinhood
  "attention-induced trading") found that stocks with extreme retail buying spikes subsequently
  **under**perform on average.
- Implication for design: be **early** on the bullish side, require price confirmation, and treat
  *extreme* attention as a potential fade, not a buy signal.

> I have not verified the exact findings, sample periods or effect sizes of these papers in this
> draft. They are cited as directional rationale only; the strategy must stand on its own paper
> track record (section 9).

**Why it may fail:** crowding (everyone runs the same scanner), bot/pump campaigns, inflated
implied volatility that eats the move, overnight gaps through stops, and very high transaction costs
relative to small position sizes.

---

## 2. Capital framework for a €100 monthly loss limit

### 2.1 The industry rule does not scale down — and why we deviate consciously

Professional systematic traders typically risk **0.5–2 % of equity per trade** and cap total open
risk at roughly 5–10 %. Applied to a €100 loss limit that would be €0.50–€2 per trade, which is less
than the commission of a single option order. **The 1–2 % rule cannot be applied to the loss limit
itself.**

Confirmed framework (v0.2):

> **The €100 is a monthly *loss limit* (net, per calendar month) on an account that holds more
> capital.** Risk control works through a hard monthly stop, a per-trade cap of about one third of
> the monthly limit (so one month needs ≥ 3 full losses to hit the stop), a small number of
> concurrent bets and strict defined-risk instruments. Account equity is not used for sizing; the
> strategy's risk is capped by the loss limit, whatever the account size.

### 2.2 Proposed limits

Assumed FX rate for this draft: **EUR/USD 1.15 → €100 ≈ USD 115** (to be replaced by the actual
rate; the system works in USD).

| Limit | Proposed value | Rationale |
|---|---|---|
| Monthly loss limit | **USD 115** (net) | €100 at assumed FX |
| Max debit (= max loss) per position | **USD 40** | ≈ 35 % of the monthly limit; ~3 full losses before the stop |
| Contracts per position | **1** | No scaling at this size |
| Max open positions | **2** | Max USD 80 at risk at any time (≈ 70 % of the limit) |
| Max positions per underlying | **1** | Concentration; meme names are correlated with each other |
| Max new entries per week | **2** | Avoids over-trading the same "hype wave" |
| Daily loss limit | **USD 40** | One full loss per day, then stop for the day |
| Monthly stop | Net realised P&L month-to-date − max loss of open positions ≤ −USD 115 → **no new entries** until the 1st trading day of next month. Open positions keep their exits. | Guarantees the month cannot lose more than €100 |
| Profits within the month | Offset losses (net limit) but do **not** increase position size. Review sizing quarterly. | No martingale, no "house money" sizing |
| Unused limit | Does **not** roll over; the counter resets each month | Keeps the monthly cap meaningful |
| Data / tool costs | **Not** counted against the €100; tracked separately (section 8) | Decided by the owner |

### 2.3 Mapping to `ibtrader` settings

| Setting | Value | Note |
|---|---|---|
| `MAX_POSITION_USD` | 40 | Sized against natural (worst-case) price by the risk engine |
| `MAX_PORTFOLIO_RISK_USD` | 80 | |
| `MAX_OPEN_POSITIONS` | 2 | |
| `MAX_DAILY_LOSS_USD` | 40 | |
| `MAX_MONTHLY_LOSS_USD` | 115 | Net realised P&L of the month minus all premium at risk; rule `monthly_loss_limit` (added in v0.3) |
| `MAX_ENTRIES_PER_WEEK` | 2 | Calendar week Monday–Sunday (New York), all producers; rule `weekly_entry_limit` (added in v0.4) |
| `MAX_UNDERLYING_EXPOSURE_USD` | 40 | Effectively one position per name |
| `MIN_OPEN_INTEREST` | 500 | Stricter than default 100: hype names have thin far-OTM strikes |
| `MAX_BID_ASK_SPREAD_PCT` | 10 | Per leg; with 0.30–1.00 premiums this is already loose in absolute terms |
| `MANUAL_APPROVAL_ABOVE_USD` | 0.01 (live phase 1) | Every live entry needs a human `approve` for the first 4 weeks |
| `MIN_CONFIDENCE_AUTO` | 0.6 | Below that, manual approval |
| `producers.yaml` → `social-momentum.budget_usd` | 80 | |
| `producers.yaml` → `status` | `paper_only` → later `manual` → `auto` | |
| `producers.yaml` → `paper_only_strategies` | `[bear_put_spread]` | Setup B stays simulated in live mode for the first 3 months (added in v0.4) |
| Signal → `exit.max_holding_days` | 10 | Time stop in NYSE sessions after the entry day (added in v0.4) |

**Coverage in the current system** (see section 10):

- The **monthly loss limit** is enforced by the risk engine (`MAX_MONTHLY_LOSS_USD`, since v0.3).
  It counts all producers on the account and uses the entry price of open positions as their
  maximum loss; commissions are not included, so keep a few dollars of headroom in mind.
- The **weekly entry cap** is enforced by the risk engine (`MAX_ENTRIES_PER_WEEK`, since v0.4).
  It counts all producers on the account; real and simulated (shadow) entries are counted
  separately, so a shadow Setup B entry does not use up the live cap. An entry counts from risk approval on, unless it ends
  rejected, expired or cancelled without a fill; an unfilled order therefore does not use up the
  week. The producer-side cap in section 3.7 stays as a second line.
- The **time stop** is a signal field (`exit.max_holding_days`, since v0.4), checked by the position
  monitor like take profit and stop loss. It fires at the first check of the session on which the
  count is reached, which can be right after the open.
- There is **no earnings / ex-dividend calendar** in the system. **Owner decision (2026-10-18):** the
  producer filters these before sending a signal (section 3.6).

---

## 3. Signal generation (producer side)

The producer is an upstream agent. It does all social-media work and emits only Markdown signals.
Nothing from Reddit or X ever reaches `ibtrader` except through the validated front matter fields
(the Markdown body is stored but never interpreted).

### 3.1 Data sources

Options compared (prices from vendor listings and third-party comparisons as of Oct 2026, **not
verified against the vendors' live pricing pages**):

| Option | What it delivers | Indicative price | Assessment |
|---|---|---|---|
| **Apify — X/Twitter actors** (e.g. `apidojo/tweet-scraper`, `kaitoeasyapi` tweet scraper) | Tweets by search query (`$TICKER`), with author and engagement fields | ~USD 0.15–0.40 per 1,000 tweets (pay per result; some actors have minimum results per query) plus Apify plan (free tier ~USD 5 platform credit/month; Starter ~USD 29/month) | **Recommended for X.** ~12–33× cheaper than the official API at our volume. Actors are third-party; quality and continuity vary. |
| **Apify — Reddit actors** | Posts and comments of a subreddit, with author, score, flair | ~USD 0.50–3.40 per 1,000 results, depending on actor; some charge per run start | Fallback for Reddit only if the cost cap allows (it does not at USD 50/month, section 8). |
| **Reddit Data API (official, OAuth)** | Posts and comments of r/wallstreetbets | Free tier for low-volume, non-commercial use (rate-limited); terms to be checked | **Recommended for Reddit** (stage 1), if personal use qualifies under the terms. |
| **Free aggregators** (e.g. ApeWisdom mention ranking) | Pre-computed WSB mention counts and ranks per ticker | Free | **Recommended as stage-1 screen and cross-check.** Methodology and uptime not under our control. |
| **Firecrawl** | Generic web page → Markdown/JSON scraping, credit-based (Free tier; Hobby ~USD 16/month; stealth mode ~5 credits per page) | — | **Not recommended for Reddit/X.** It is a page scraper, not a social-data API (no search by cashtag, no pagination over comments), and several reports say Firecrawl blocks major social-media domains. Useful for secondary tasks: confirming earnings dates or reading company IR/news pages. |
| **X API (official), pay-per-use since Feb 2026** | Search and read posts | ~USD 0.005 per post read → 44,000 posts/month ≈ **USD 220** | **Not recommended** at this cost level. Reportedly, commercial use still requires the Enterprise plan; personal-use status to be clarified. |
| Market data | Price, volume, option chain | IBKR (already integrated) | — |

**Proposed two-stage pipeline:**

1. **Stage 1 (broad, free):** every trading day, mention counts for all tickers from the Reddit API
   (r/wallstreetbets posts plus the daily discussion thread) and an aggregator as a cross-check.
   Compute `attention_z`, `attention_accel` and `rank_wsb` (section 3.3). Output: at most
   **10 candidate tickers** that pass the universe filters (section 3.6) and A1/A2 or B1/B2.
2. **Stage 2 (targeted, paid):** for each candidate, pull up to **200 recent tweets** for `$TICKER`
   via an Apify actor (≈ 2,000 tweets per day, ≈ 44,000 per month) and up to 100 WSB comments on
   the posts that mention the ticker via the Reddit API. The Apify Reddit actor is not used: it
   would break the USD 50 cost cap (section 8). Compute breadth, `bot_score` and sentiment on this
   sample.

**Legal and terms-of-service note.** Both X and Reddit prohibit unauthorised scraping in their
terms of service and have taken legal action against scrapers and data resellers. Using a
third-party Apify actor does not move this risk away from the user of the data. For a personal,
non-redistributed research use the practical risk is mainly account blocking or the actor ceasing
to work, but this is **not a legal assessment**. **Owner decision (2026-10-18): risk accepted;
X and Reddit data via Apify/Reddit API as described.**

### 3.2 Ticker extraction

- Accept a ticker only if written as cashtag (`$GME`) **or** an exact match against a whitelist of
  US-listed stocks **with listed options**.
- Blacklist common words that are also tickers: `A, ALL, IT, ON, DD, YOLO, CEO, USA, FOR, ARE, NOW,
  OPEN, BIG, GO, BE, SO, RH (ambiguous)` … (maintain as a list).
- Exclude ETFs and indices (SPY, QQQ, …): the strategy targets single-stock attention.

### 3.3 Features (computed hourly, aggregated daily at 15:00 ET)

| Feature | Definition |
|---|---|
| `mentions_24h` | Count of posts + comments (WSB) and posts (X) mentioning the ticker in the last 24 h |
| `attention_z` | Robust z-score of `mentions_24h` vs. the ticker's own last 30 days: (x − median) / (1.4826 × MAD) |
| `attention_accel` | `mentions_24h` / `mentions_prev_24h` |
| `unique_authors_24h` | Distinct authors; `breadth = unique_authors / mentions` |
| `sentiment` | Net share of bullish minus bearish mentions, −1 … +1, classified by a language model or a finance sentiment model; weight authors by account age and karma |
| `rank_wsb` | Rank by mentions among all tickers in WSB, last 24 h |
| `bot_score` | Share of mentions from accounts < 30 days old or with near-identical text |

### 3.4 Setup A — Early momentum (bullish → `bull_call_spread`)

All conditions must hold:

| # | Condition | Proposed threshold |
|---|---|---|
| A1 | Attention is accelerating | `attention_z ≥ 2.0` **and** `attention_accel ≥ 1.5` |
| A2 | Not yet peak hype | `rank_wsb` between 3 and 25 (top 1–2 is often already late) and `attention_z < 6` |
| A3 | Broad, organic discussion | `unique_authors_24h ≥ 50`, `breadth ≥ 0.3`, `bot_score ≤ 0.25` |
| A4 | Sentiment bullish | `sentiment ≥ +0.30` |
| A5 | Price/volume confirmation (checked by `ibtrader`) | `close > sma_20 and close > ema_10 and rsi_14 >= 50 and rsi_14 <= 75 and volume > 1.5 * avg_volume_20` |

### 3.5 Setup B — Exhaustion fade (bearish → `bear_put_spread`)

| # | Condition | Proposed threshold |
|---|---|---|
| B1 | Attention extreme for ≥ 2 days | `attention_z ≥ 4` on 2 consecutive days, `rank_wsb ≤ 3` |
| B2 | Attention starts to fade | `attention_accel ≤ 0.8` (today below yesterday) |
| B3 | Price overextended | Up > 30 % in the last 5 trading days (producer computes) |
| B4 | Price rolling over (checked by `ibtrader`) | `rsi_14 >= 65 and close < ema_5 and close < open` |
| B5 | Squeeze risk acceptable | Short interest < 25 % of float, not on hard-to-borrow list (producer, data source to be defined) |

Setup B is the riskier one (short squeezes). **Owner decision (2026-10-18): B runs in paper only
for the first 3 months**, even if A goes live. Enforced by the producer registry entry
`paper_only_strategies: [bear_put_spread]`: in `MODE=live` these entries are simulated (shadow),
whatever the producer status.

### 3.6 Universe filters (both setups)

| Filter | Value | Why |
|---|---|---|
| Underlying price | USD 8 – 60 | Below: penny-stock dynamics; above: a sensible spread costs more than USD 40 |
| Market cap | ≥ USD 300 m | Avoids micro-cap pump & dumps |
| Avg. daily volume (20 d) | ≥ 2 m shares | Liquidity |
| Options | Weekly or monthly expiries in the 21–45 DTE window, strike increments ≤ USD 1 (≤ 2.5 above USD 40) | Needed to build a ≤ USD 40 spread |
| Earnings | **No earnings date before planned exit** (i.e. within the next 45 days, or close before) | IV crush and gap risk |
| Ex-dividend | No ex-date within holding period (call spreads) | Early-assignment risk of the short call |
| Corporate events | No pending M&A, offerings, reverse splits, halts in last 5 days, IPO < 90 days, SPACs | Unmodelled jumps |
| Data quality | Attention data available for ≥ 30 days | z-score needs a baseline |

### 3.7 From score to signal

- `confidence` = a calibrated score 0–1 from the features (initially a simple weighted sum; to be
  calibrated on paper data). Below 0.5 → no signal. 0.5–0.6 → signal, but manual approval.
- Max **one** signal per ticker per 5 trading days (cool-down).
- Max **two** new signals per week across all tickers (producer-side cap, mirrors section 2.2).

---

## 4. Execution timing

- The producer runs at **15:00 ET** on trading days (social data of the last 24 h, plus intraday
  price).
- It emits a `rule` with `trigger.check_at: "15:30 America/New_York"` and a price/volume condition.
  `valid_until` is the same day 15:44 ET, `max_entries_per_day: 1` → exactly one evaluation.
- **Why late in the session:** IBKR daily bars requested during the session include today's
  *partial* bar. At 10:30 ET today's `volume` is only a fraction of the day, so a
  `volume > 1.5 * avg_volume_20` condition would almost never be true. At 15:30 ET the bar is
  ~90 % complete and opening-auction noise is gone. (Assumption to verify on paper — section 10.)
- Orders: limit at mid, `max_reprice_steps: 3` toward natural, never above the risk engine's
  max price. Unfilled → cancelled. No chasing.
- **Pattern Day Trader rule:** entries late in the day and a minimum holding of overnight reduce
  day trades. Stops on the entry day can still create day trades; to be checked against the
  account's PDT status (section 10).

---

## 5. Instrument and contract selection

| Parameter | Proposal | Rationale |
|---|---|---|
| Strategy | `bull_call_spread` (A), `bear_put_spread` (B) | Defined risk = debit paid; short leg finances part of the inflated IV |
| Not used | Naked short calls/puts | Unlimited/large loss, margin, not allowed by the system |
| Not used | Single `long_call` / `long_put` | Pays full inflated IV; vega and theta losses after the hype fades |
| DTE | `dte_min: 21`, `dte_max: 45` | Enough time for the move; less theta decay than weeklies; avoids gamma risk |
| Long leg delta | 0.50 | At the money: best balance between probability and cost |
| Short leg delta | 0.25 | Spread width usually 1–2 strikes on USD 8–60 names |
| Target debit | ≤ 45 % of spread width | Reward:risk ≥ 1.2 : 1 at expiry. **The system cannot enforce this today** (it selects by delta, not by width); the producer must pre-check with a chain snapshot, and the USD 40 cap rejects too-wide spreads. |
| Order | One BAG combo, limit only | Enforced by the system |

Example: stock at USD 20, 30 DTE, buy 20 call / sell 22 call for USD 0.80 debit → max loss
USD 80 → **rejected** by the USD 40 cap. Buy 20 / sell 21 for USD 0.40 → max loss USD 40,
max gain USD 60 → accepted. This shows the practical consequence of the USD 40 cap: **only USD 1-wide
spreads on lower-priced names are tradable.** Note that the risk engine checks the cap against the
*natural* price (pay the ask), not the mid: a spread quoted 0.35 / 0.45 with mid 0.40 counts as
USD 45 and is rejected. In practice the mid debit must be about USD 0.30–0.35.

---

## 6. Exit rules

| Exit | Rule | Implemented by |
|---|---|---|
| Take profit | **+75 %** of entry debit (≈ 50 % of max gain for a 0.40 debit / 1.00 width) | `exit.take_profit_pct: 75` |
| Stop loss | **−50 %** of entry debit (mid-based) | `exit.stop_loss_pct: 50` |
| Expiry | Close when **≤ 7 days** to expiry, whatever the P&L | `exit.close_before_expiry_days: 7` |
| Time stop | Close after **10 trading days** if neither TP nor SL was hit | `exit.max_holding_days: 10` (sessions after the entry day) |
| Thesis broken (A) | `attention_z` falls below 0.5 **or** sentiment turns ≤ 0 **or** close < `sma_20` | Producer sends `exit` |
| Thesis broken (B) | New price high above the entry-day high **or** attention re-accelerates (`attention_accel ≥ 1.5`) | Producer sends `exit` |
| Partial profit | Not used (1 contract) | — |
| Emergency | Kill switch (stops entries; exits continue) | Operator |

Notes:

- A spread's mid price is noisy; a −50 % stop can trigger on a wide quote, and gaps can cause losses
  up to −100 %. **Plan with −100 % as the true worst case** — that is what the USD 40 cap does.
- **Never hold through expiry.** If the long leg were exercised, the account would need cash for
  100 shares (e.g. USD 2,000 for a USD 20 stock); IBKR would liquidate positions itself.

---

## 7. Expected economics (illustrative, not a forecast)

Per trade, USD 40 debit, TP +75 %, SL −50 %, round-trip commissions ≈ USD 2.60 (4 option legs at
the IBKR Pro fixed rate of about USD 0.65 per contract; exchange/regulatory fees extra — to be
verified for the account's pricing plan):

| | USD |
|---|---|
| Win (+75 %) | +30.00 − 2.60 = **+27.40** |
| Loss (−50 %) | −20.00 − 2.60 = **−22.60** |
| Break-even hit rate | 22.60 / (27.40 + 22.60) = **45.2 %** |

With slippage, gaps and time-stop exits, the realistic break-even hit rate is closer to **50 %**.
At 4 trades per month, monthly results range from about −USD 90 (4 losses) to +USD 110 (4 wins);
one full −100 % gap loss costs USD 42.60. Commissions alone are ~6.5 % of each position.

---

## 8. Running costs (on top of the €100 loss limit, capped at USD 50/month)

These costs are **not** counted against the €100 monthly loss limit (owner decision, v0.2). They
are capped at **USD 50 per month** (owner decision, v0.3) and tracked separately so that the
strategy's true economics stay visible.

Volume basis: 10 candidate tickers × 200 tweets × ~22 trading days ≈ **44,000 tweets/month**;
WSB posts plus up to 100 comments per candidate (≈ 22,000 comments/month) via the Reddit API.

| Cost item | Sub-cap (USD/month) | Basis / status |
|---|---|---|
| Apify Starter plan (covers X actor usage) | 29 | 44,000 tweets × 0.25–0.40 per 1,000 ≈ 11–18 of usage, inside the plan's included usage. **To verify** on apify.com. |
| LLM for sentiment classification | 6 | ≈ 66,000 short texts per month, small model, batched. Confirm the real cost per 1,000 texts in phase 0. |
| IBKR real-time US option quotes (OPRA) and US securities bundle (non-professional) | 10 | Partly waived above a monthly commission threshold. **To verify** in the account. |
| Hosting (IB Gateway + producer) | 5 | Smallest VM tier or an existing machine. Open (ASSUMPTIONS A.5). |
| Reddit Data API, aggregator, Firecrawl free tier | 0 | Free tiers only. **No** paid Firecrawl plan and **no** Apify Reddit actor within the cap. |
| **Total** | **50** | |
| *For comparison: official X API instead of Apify* | *~220* | *44,000 posts × 0.005* |

**Cost guard (producer-side).** The producer records its spend per day (Apify usage from the run
results, LLM tokens, fixed items pro rata) and projects the month. If the projection exceeds
USD 50, it degrades in this order and logs each step:

1. Tweets per candidate 200 → 100.
2. Candidate tickers per day 10 → 5.
3. X stage off: Reddit-only signals, `confidence` capped at 0.55 so that every entry needs manual
   approval (`MIN_CONFIDENCE_AUTO` 0.6, section 2.3).

If IBKR market data or hosting alone exceed their sub-caps, the cost guard cannot compensate:
the operator decides (raise the cap or reduce the set-up).

**Consequence:** USD 50 per month is about 43 % of the monthly loss limit. At the illustrative
economics of section 7 (4 trades per month), the strategy needs an average trading profit of about
**USD 12.50 per trade** just to cover running costs. **Recommendation:** run phase 0 and the paper
phase with the same cost guard, so that the paper results show whether the strategy pays for its
data.

---

## 9. Validation and go-live plan

| Phase | Duration | Mode | Gate to the next phase |
|---|---|---|---|
| 0. Data | 4 weeks | Producer only, no signals sent; log features daily | Baseline for z-scores; manual review of 20 detected tickers for false positives |
| 1. Paper | ≥ 8 weeks **and** ≥ 30 signals | `MODE=paper`, producer `paper_only` | Hit rate ≥ 50 %, profit factor ≥ 1.3 after trading costs, average profit per trade covers the running costs of section 8, max drawdown ≤ USD 115, ≥ 80 % of entry orders filled within 3 reprices, no unexplained system errors |
| 2. Live, manual | 4 weeks | `MODE=live`, producer `manual`, Setup A only (Setup B shadow via `paper_only_strategies`) | Live results within the paper range; slippage vs. mid ≤ 10 % of debit |
| 3. Live, auto | ongoing | producer `auto`, approval threshold restored | Monthly review |

**Strategy-level kill criteria** (pause the producer, review the design):

- Monthly loss limit hit in **2 consecutive months**.
- Running costs exceed trading profit over a rolling **3 months** (after go-live).
- Rolling 20-trade hit rate < 35 %.
- Average slippage > 15 % of debit.
- Social-media data feed missing or stale > 1 trading day (fail closed: no signals).
- Any sign that signals are derived from coordinated/manipulative campaigns (`bot_score` spike).

---

## 10. Open points for your decision

These are not assumptions I filled in silently; each needs your input or a check on the account.
Decided items are kept for traceability.

| # | Question / check | Why it matters |
|---|---|---|
| 1 | ~~What exactly is the €100?~~ **Decided (v0.2):** monthly net loss limit on a larger account. | — |
| 2 | ~~Account type at IBKR~~ **Decided (v0.3):** margin account confirmed. Still to check on paper: the options permission covers vertical spreads (one paper order per strategy, ASSUMPTIONS B.1). | — |
| 3 | **Pattern Day Trader status** of the account (applies to US-regulated margin accounts < USD 25,000; rule changes have been discussed by FINRA — current status to be verified). | Stops on entry day count as day trades. |
| 4 | **Base currency and FX.** EUR base: buy USD in advance (manual conversion) to avoid an automatic USD margin loan and interest. Confirm the FX rate used to convert the loss limit. | The system's limits are in USD. |
| 5 | ~~Data budget~~ **Decided (v0.2/v0.3):** on top of the €100; terms-of-service risk of Apify scraping accepted; running costs capped at USD 50/month (section 8). | — |
| 6 | ~~Setup B (bearish fade) paper only for the first 3 months~~ **Decided (v0.4):** yes; enforced by `paper_only_strategies: [bear_put_spread]`. Remove the entry after 3 months only if B meets the phase-1 gates on its own. | — |
| 7 | ~~Monthly loss limit, weekly entry cap, max holding days~~ **Done:** `MAX_MONTHLY_LOSS_USD` (v0.3), `MAX_ENTRIES_PER_WEEK` and `exit.max_holding_days` (v0.4). **Decided (v0.4):** earnings / ex-dividend filter stays with the producer. | — |
| 8 | **Personal account dealing.** Does your employer's compliance policy for private securities transactions (pre-clearance, holding periods, restricted list) apply to automated trading? | A 10-day time stop or a stop loss may conflict with a minimum holding period. |
| 9 | **Tax treatment** (Germany: capital income, reporting of a foreign broker account in the tax return) — not assessed in this draft. | No tax statements are made here. |
| 10 | **Partial-bar behaviour** of IBKR daily bars at 15:30 ET, as assumed in section 4. | If the current day is not included, the conditions refer to yesterday's bar. |

---

## Appendix A — Example signal (Setup A)

Validated with `ibtrader validate` against Signal Contract 1.0 (see `examples/signals/` for
the format; this file is illustrative and not part of the example set).

```markdown
---
contract_version: "1.0"
signal_id: "socmom-a-sofi-2026-10-19-001"
version: 1
kind: rule
producer: "social-momentum"
created_at: "2026-10-19T19:00:00Z"
valid_until: "2026-10-19T19:44:00Z"
confidence: 0.64
underlying: "SOFI"
strategy: bull_call_spread
contract:
  dte_min: 21
  dte_max: 45
  long_leg_delta: 0.50
  short_leg_delta: 0.25
order:
  type: limit
  price: mid
  max_reprice_steps: 3
sizing:
  max_premium_usd: 40
  max_contracts: 1
exit:
  take_profit_pct: 75
  stop_loss_pct: 50
  close_before_expiry_days: 7
  max_holding_days: 10
trigger:
  type: condition
  condition: "close > sma_20 and close > ema_10 and rsi_14 >= 50 and rsi_14 <= 75 and volume > 1.5 * avg_volume_20"
  check_at: "15:30 America/New_York"
max_entries_per_day: 1
---

## Rationale
Setup A (early momentum). attention_z 2.7, accel 1.9, rank_wsb 9, unique authors 140,
breadth 0.41, bot_score 0.08, sentiment +0.44. No earnings before planned exit.
```

The ticker is a placeholder for illustration, not a recommendation.

## Appendix B — Example thesis exit

```markdown
---
contract_version: "1.0"
signal_id: "socmom-exit-sofi-2026-10-27-001"
version: 1
kind: exit
producer: "social-momentum"
created_at: "2026-10-27T15:00:00Z"
valid_until: "2026-10-27T20:00:00Z"
confidence: 0.9
target_signal_id: "socmom-a-sofi-2026-10-19-001"
action: close
---

## Rationale
Thesis broken: attention_z 0.3 (< 0.5), sentiment -0.05.
```
