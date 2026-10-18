# Related open-source projects

Survey of public repositories relevant to ib-ai-trader, as of **2026-10-18**. Figures come from
the GitHub and PyPI pages on that date. Stars are rounded the way GitHub shows them. Where a page
did not show a value, the table says "not shown". This is a desk review of READMEs and project
pages: no code was audited. Check licence and maintenance status again before adopting anything.

## Summary

| Project | Role for us | Licence | Signal | Status |
|---|---|---|---|---|
| [ib-api-reloaded/ib_async](https://github.com/ib-api-reloaded/ib_async) | IBKR API client | BSD-2-Clause | ~1.8k stars | **in use** (`ibkr.py`) |
| [gnzsnz/ib-gateway-docker](https://github.com/gnzsnz/ib-gateway-docker) | IB Gateway + IBC container | MIT | ~1.3k stars | **in use** (`docker-compose.yml`) |
| [IbcAlpha/IBC](https://github.com/IbcAlpha/IBC) | automated Gateway login/restart | GPL-3.0 | ~1.6k stars | **retired, archived 2026-09-01**: action needed |
| [rsheftel/pandas_market_calendars](https://github.com/rsheftel/pandas_market_calendars) | NYSE session calendar | MIT | ~1.0k stars | **in use** (`clock.py`) |
| [gerrymanoim/exchange_calendars](https://github.com/gerrymanoim/exchange_calendars) | calendars backing the above (XNYS) | Apache-2.0 | 680 stars | transitive dependency |
| [baileywickham/ibkr](https://github.com/baileywickham/ibkr) | reference: options CLI with combo orders | MIT | 1 star | reference only |
| [Hellek1/ib-mcp](https://github.com/Hellek1/ib-mcp) | read-only IBKR data MCP server | BSD-3-Clause | v0.3.1 (2026-09-18) | candidate for upstream agents |
| [kelvingao/ibkr-mcp](https://github.com/kelvingao/ibkr-mcp) | MCP server: portfolio risk, option scans | **proprietary** | 5 stars | reference only, no reuse |
| [karimQuant/ibapi-mcp-server](https://github.com/karimQuant/ibapi-mcp-server) | MCP server (portfolio tool only) | MIT | 4 stars | not relevant yet |
| [QuantInsti/AI-in-Trading-Workflow](https://github.com/QuantInsti/AI-in-Trading-Workflow) | LLM agents + IBKR examples | Apache-2.0 | 16 stars | idea source for upstream agents |
| [letianzj/quanttrader](https://github.com/letianzj/quanttrader) | event-driven backtest/live framework (IB) | Apache-2.0 (sidebar) | 784 stars | not adopted |
| [ranaroussi/ezibpy](https://github.com/ranaroussi/ezibpy) | old IbPy wrapper | Apache-2.0 | 334 stars | **archived 2024-01-12**, do not use |

## In use

### ib_async: IBKR API client
Sync/async framework for the TWS API and the successor of `ib_insync`. Requires Python 3.10 or
later; we run 2.1.0 from PyPI. The GitHub releases page lists v2.0.1 as its latest release, so
PyPI is ahead of the GitHub release notes. `ibkr.py` is the only module that imports it.
**Action:** pin a tested version range after the paper-account integration run, and re-test on
each upgrade. `ibkr.py` has no automated tests.

### gnzsnz/ib-gateway-docker: Gateway container
Runs IB Gateway or TWS headless with IBC, Xvfb, an optional VNC server, and socat relays. The
README confirms the ports our compose file uses: **4003 = live** (relayed from 4001) and
**4004 = paper** (relayed from 4002). `TRADING_MODE` takes `live`, `paper` or `both`.
`TWOFA_TIMEOUT_ACTION=restart` is recommended together with `AUTO_RESTART_TIME`. Image tags at the
time of review: `latest` = Gateway 10.51.1c with IBC 3.24.2, `stable` = 10.50.1f.
**Action:** pin an image digest; follow issue
[#366 "IBC deprecation"](https://github.com/gnzsnz/ib-gateway-docker/issues/366).

### pandas_market_calendars: session calendar
Provides holiday, early-close and late-open schedules. Since v2.0 it mirrors the
`exchange_calendars` calendars, and it computes them locally from rules with no runtime server
query. Adopted in `clock.py` in place of the hand-written 2026–2027 table. Its dates matched that
table exactly, and they are pinned in `tests/test_clock.py`. Cost: pulls in pandas.
**Action:** keep it updated, because unscheduled closures arrive only through new releases.

## Action needed

### IbcAlpha/IBC: retired
IBC automates the Gateway/TWS login, takes part in IBKR Mobile 2FA, handles dialogs and restarts
the gateway daily without re-authentication. Per its README the project was **retired and
archived on 1 September 2026**. Only work in progress and significant bug fixes will be
completed; issues and PRs are closed, and support has moved to the
[IBC user group](https://groups.io/g/ibcalpha). The gateway image above still bundles it (3.24.2).

Consequences for us: the automated-login part of the hosting plan (spec section 2, "IB Gateway
with IBC") has no maintained upstream. It keeps working until an IBKR Gateway change breaks it.
Options:
1. Keep the bundled IBC for now and watch gnzsnz issue #366 for the image's replacement.
2. Evaluate [zalkabsi/IBC2.0](https://github.com/zalkabsi/IBC2.0). It is a self-described
   maintained fork plus a supervisor (watchdog, alerts, Docker image). Its README reports only a
   limited manual check up to the login window. **Not reviewed; unproven.**
3. Accept manual login after each forced re-authentication, with alerting on disconnect.

This decision belongs with spec open question 5 (hosting and who owns Gateway maintenance), see
[ASSUMPTIONS.md](ASSUMPTIONS.md).

## References (not dependencies)

### baileywickham/ibkr
A small options and stock CLI on `ib_async`. Vertical spreads go out as native combo orders,
orders are limit only (no market orders by design), every order goes through a one-shot preview →
confirm → execute token, and paper is the default (`--live` to switch). The design is close to
ours, but adoption is minimal (1 star, 14 commits), and the README does not describe how BAG/combo
legs or tick sizes are handled. **Use:** compare its combo construction with `ibkr.IBKRBroker`
during the integration test. No dependency.

### Hellek1/ib-mcp
MCP server on `ib_async` + FastMCP that exposes IBKR data as read-only. It offers contracts,
history, option chains and quotes, news, fundamentals and portfolio, with no order placement per
its security section. **Use:** a data source for the *upstream* signal agents, which keeps
execution exclusively in ib-ai-trader behind the risk engine. The read-only claim has not been
verified in its source.

### kelvingao/ibkr-mcp
MCP server for portfolio management, option strategy scans (covered calls, iron condors, PMCC,
verticals) and risk limits in `risk.yaml` (max delta, theta, concentration). No order-placement
tool is listed. The README states **"Proprietary – All rights reserved"** although the repository
is public. **Use:** design ideas only (Greek-based portfolio limits); the code must not be copied.

### karimQuant/ibapi-mcp-server
MCP server whose only documented tool is `get_portfolio_tool`. The README says it could
"potentially execute trades". Not relevant at this stage.

### QuantInsti/AI-in-Trading-Workflow
Examples of an agent-based portfolio manager, an MCP server on the IBKR API, and a
news-sentiment multi-agent strategy. Small (16 stars), course material. **Use:** inspiration for
the upstream agents, which are out of scope here (spec section 1).

### letianzj/quanttrader
Event-driven backtest and live-trading framework with IB support, including options. Not adopted:
its execution model would replace our gateway → risk → order-manager pipeline instead of serving
it. The README licence badge links to MIT while the sidebar shows Apache-2.0, so check before any
reuse. Possibly useful later for backtesting producer rules.

### ranaroussi/ezibpy
Archived on 2024-01-12 and built on the deprecated IbPy. Its combo-order example (bear call
spread) is a historical reference only. Do not use.

## Not verified
- `ibkrbox` (PyPI, box-spread combo builder): the PyPI page did not load during review, so it is
  not listed above.
- Last-commit dates: most GitHub pages did not show them in the captured content.
