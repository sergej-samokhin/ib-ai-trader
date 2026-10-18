"""IBKR connection, market data and broker adapters on top of `ib_async`.

Not covered by unit tests (needs IB Gateway). Validate against a paper account; see
README "Integration testing" and the assumptions list in docs/ASSUMPTIONS.md.
"""

from __future__ import annotations

import logging
import math
import time as _time
from datetime import date, datetime

from ib_async import IB, ComboLeg, Contract, LimitOrder, Option, Stock

from ibtrader.config import Settings
from ibtrader.domain import Bar, Leg, OptionQuote, Right
from ibtrader.orders import BrokerOrderStatus

log = logging.getLogger("ibtrader.ibkr")

QUOTE_WAIT_SECONDS = 2.5
MAX_STRIKES_PER_REQUEST = 24  # strikes per side around spot; keeps market-data lines low
QUOTE_BATCH = 40  # simultaneous market-data lines per batch (pacing)
CACHE_SECONDS = 30.0


class IBKRConnection:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.ib = IB()

    def connect(self, attempts: int = 5) -> None:
        delay = 2.0
        for attempt in range(1, attempts + 1):
            try:
                self.ib.connect(
                    self.settings.ib_host,
                    self.settings.ib_port,
                    clientId=self.settings.ib_client_id,
                    timeout=10,
                    account=self.settings.ib_account,
                )
                self.ib.reqMarketDataType(self.settings.market_data_type)
                self._verify_account()
                log.info("connected to IBKR", extra={"fields": {"attempt": attempt}})
                return
            except PermissionError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "IBKR connect failed", extra={"fields": {"attempt": attempt, "error": str(exc)}}
                )
                if attempt == attempts:
                    raise
                _time.sleep(delay)
                delay = min(delay * 2, 60)

    def _verify_account(self) -> None:
        accounts = self.ib.managedAccounts()
        account = self.settings.ib_account or (accounts[0] if len(accounts) == 1 else "")
        if not account or account not in accounts:
            self.ib.disconnect()
            raise PermissionError(f"IB_ACCOUNT {account!r} not in managed accounts {accounts}")
        is_paper_account = account.startswith("DU")
        if self.settings.mode == "paper" and not is_paper_account:
            self.ib.disconnect()
            raise PermissionError(
                f"MODE=paper but connected account {account} is not a paper account"
            )
        if self.settings.mode == "live" and is_paper_account:
            self.ib.disconnect()
            raise PermissionError(f"MODE=live but connected account {account} is a paper account")
        self.account = account

    def ensure_connected(self) -> bool:
        """Reconnect if needed. Returns True if a reconnect happened (caller re-runs recovery)."""
        if self.ib.isConnected():
            return False
        log.warning("IBKR disconnected, reconnecting")
        self.connect()
        return True

    def sleep(self, seconds: float) -> None:
        self.ib.sleep(seconds)


class IBKRMarketData:
    def __init__(self, conn: IBKRConnection):
        self.conn = conn
        self.ib = conn.ib
        self._stocks: dict[str, Stock] = {}
        self._chains: dict[str, tuple[float, object]] = {}
        self._quotes: dict[tuple, tuple[float, list[OptionQuote]]] = {}

    def _stock(self, symbol: str) -> Stock:
        if symbol not in self._stocks:
            stock = Stock(symbol, "SMART", "USD")
            self.ib.qualifyContracts(stock)
            self._stocks[symbol] = stock
        return self._stocks[symbol]

    def daily_bars(self, symbol: str, count: int) -> list[Bar]:
        years = max(1, math.ceil(count / 250))
        bars = self.ib.reqHistoricalData(
            self._stock(symbol),
            endDateTime="",
            durationStr=f"{years} Y",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
        )
        out = [
            Bar(
                b.date if isinstance(b.date, date) else b.date.date(),
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
            )
            for b in bars
        ]
        return out[-count:]

    def _chain(self, symbol: str):
        cached = self._chains.get(symbol)
        if cached and _time.monotonic() - cached[0] < 3600:
            return cached[1]
        stock = self._stock(symbol)
        chains = self.ib.reqSecDefOptParams(stock.symbol, "", stock.secType, stock.conId)
        smart = [c for c in chains if c.exchange == "SMART"]
        chain = next((c for c in smart if c.tradingClass == symbol), smart[0] if smart else None)
        if chain is None:
            raise LookupError(f"no SMART option chain for {symbol}")
        self._chains[symbol] = (_time.monotonic(), chain)
        return chain

    def expirations(self, symbol: str) -> list[date]:
        return sorted(
            datetime.strptime(e, "%Y%m%d").date() for e in self._chain(symbol).expirations
        )

    def _spot(self, symbol: str) -> float:
        ticker = self.ib.reqMktData(self._stock(symbol), "", False, False)
        self.ib.sleep(QUOTE_WAIT_SECONDS)
        price = ticker.marketPrice()
        self.ib.cancelMktData(self._stock(symbol))
        if price is None or math.isnan(price):
            price = ticker.close
        if price is None or math.isnan(price):
            raise LookupError(f"no price for {symbol}")
        return float(price)

    def _quote_contracts(self, contracts: list[Option]) -> list[OptionQuote]:
        quotes: list[OptionQuote] = []
        for i in range(0, len(contracts), QUOTE_BATCH):
            batch = contracts[i : i + QUOTE_BATCH]
            tickers = [self.ib.reqMktData(c, "100,101,106", False, False) for c in batch]
            self.ib.sleep(QUOTE_WAIT_SECONDS)
            for contract, ticker in zip(batch, tickers, strict=True):
                greeks = ticker.modelGreeks
                oi = ticker.callOpenInterest if contract.right == "C" else ticker.putOpenInterest
                quotes.append(
                    OptionQuote(
                        symbol=contract.symbol,
                        expiry=datetime.strptime(
                            contract.lastTradeDateOrContractMonth, "%Y%m%d"
                        ).date(),
                        strike=float(contract.strike),
                        right=contract.right,  # type: ignore[arg-type]
                        bid=_num(ticker.bid),
                        ask=_num(ticker.ask),
                        delta=_num(greeks.delta, signed=True) if greeks else None,
                        open_interest=int(oi) if _num(oi) is not None else None,
                        con_id=contract.conId,
                    )
                )
                self.ib.cancelMktData(contract)
        return quotes

    def option_quotes(self, symbol: str, expiry: date, right: Right) -> list[OptionQuote]:
        key = (symbol, expiry, right)
        cached = self._quotes.get(key)
        if cached and _time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        chain = self._chain(symbol)
        spot = self._spot(symbol)
        strikes = sorted(chain.strikes, key=lambda k: abs(k - spot))[: MAX_STRIKES_PER_REQUEST * 2]
        contracts = [
            Option(
                symbol,
                expiry.strftime("%Y%m%d"),
                k,
                right,
                "SMART",
                tradingClass=chain.tradingClass,
                multiplier=chain.multiplier,
            )
            for k in sorted(strikes)
        ]
        qualified = [c for c in self.ib.qualifyContracts(*contracts) if c and c.conId]
        quotes = self._quote_contracts(qualified)
        self._quotes[key] = (_time.monotonic(), quotes)
        return quotes

    def quote_legs(self, legs: list[Leg]) -> list[OptionQuote]:
        contracts = []
        for leg in legs:
            contract = (
                Option(conId=leg.con_id, exchange="SMART")
                if leg.con_id
                else Option(
                    leg.symbol, leg.expiry.strftime("%Y%m%d"), leg.strike, leg.right, "SMART"
                )
            )
            contracts.append(contract)
        qualified = self.ib.qualifyContracts(*contracts)
        return self._quote_contracts(qualified)


class IBKRBroker:
    """Places limit orders; spreads as a single BAG combo order, never as separate legs."""

    def __init__(self, conn: IBKRConnection):
        self.conn = conn
        self.ib = conn.ib
        self._trades: dict[str, object] = {}

    def _contract(self, legs: list[Leg]) -> Contract:
        if len(legs) == 1:
            leg = legs[0]
            contract = Option(conId=leg.con_id, exchange="SMART")
            self.ib.qualifyContracts(contract)
            return contract
        return Contract(
            secType="BAG",
            symbol=legs[0].symbol,
            currency="USD",
            exchange="SMART",
            comboLegs=[
                ComboLeg(conId=leg.con_id, ratio=leg.ratio, action=leg.action, exchange="SMART")
                for leg in legs
            ],
        )

    def submit(self, legs, action, quantity, limit_price, order_ref) -> str:
        if any(leg.con_id is None for leg in legs):
            raise ValueError("all legs need a conId")
        if limit_price <= 0:
            raise ValueError("limit price must be positive")
        order = LimitOrder(
            action,
            quantity,
            limit_price,
            orderRef=order_ref,
            tif="DAY",
            account=self.conn.account,
            transmit=True,
        )
        trade = self.ib.placeOrder(self._contract(legs), order)
        order_id = str(trade.order.orderId)
        self._trades[order_id] = trade
        return order_id

    def _trade(self, order_id: str):
        trade = self._trades.get(order_id)
        if trade is None:
            for candidate in self.ib.openTrades() + self.ib.trades():
                if str(candidate.order.orderId) == order_id:
                    self._trades[order_id] = candidate
                    return candidate
            raise LookupError(f"unknown order {order_id}")
        return trade

    def modify(self, order_id: str, limit_price: float) -> None:
        trade = self._trade(order_id)
        trade.order.lmtPrice = limit_price
        self.ib.placeOrder(trade.contract, trade.order)

    def cancel(self, order_id: str) -> None:
        self.ib.cancelOrder(self._trade(order_id).order)

    def poll(self, order_id: str) -> BrokerOrderStatus:
        trade = self._trade(order_id)
        status = trade.orderStatus.status
        filled = int(trade.orderStatus.filled or 0)
        avg = _num(trade.orderStatus.avgFillPrice) or None
        message = "; ".join(e.message for e in trade.log if e.errorCode)[-500:]
        if status == "Filled":
            return BrokerOrderStatus("filled", filled, avg, message)
        if status in ("Cancelled", "ApiCancelled"):
            return BrokerOrderStatus("cancelled", filled, avg, message)
        if status == "Inactive":
            return BrokerOrderStatus("rejected", filled, avg, message)
        return BrokerOrderStatus("working", filled, avg, message)

    def find_by_ref(self, order_ref: str) -> str | None:
        for trade in self.ib.openTrades():
            if trade.order.orderRef == order_ref:
                order_id = str(trade.order.orderId)
                self._trades[order_id] = trade
                return order_id
        return None

    def lookup_final(self, order_ref: str) -> BrokerOrderStatus | None:
        """Completed orders and executions IBKR sends at connect time (current trading day only).

        Spreads: the net combo price is taken from BAG-level executions. If only leg executions
        exist for a multi-leg order the net price is ambiguous and None is returned (stays
        `unknown`, operator resolves it).
        """
        trades = [t for t in self.ib.trades() if t.order.orderRef == order_ref]
        fills = [f for f in self.ib.fills() if f.execution.orderRef == order_ref]
        if not trades and not fills:
            return None
        bag = [f for f in fills if f.contract.secType == "BAG"]
        legs = {f.contract.conId for f in fills if f.contract.secType != "BAG"}
        counted = bag or (fills if len(legs) <= 1 else [])
        if fills and not counted:
            return None
        filled = sum(float(f.execution.shares) for f in counted)
        avg = (
            sum(float(f.execution.shares) * float(f.execution.price) for f in counted) / filled
            if filled
            else None
        )
        status = trades[-1].orderStatus.status if trades else ""
        reported = max(
            [float(trades[-1].orderStatus.filled or 0), _num(trades[-1].order.filledQuantity) or 0]
            if trades
            else [0.0]
        )
        if reported > filled:
            return None  # IBKR says it filled, but we lack the executions (price): stay unknown
        if status in ("PreSubmitted", "Submitted", "PendingSubmit", "PendingCancel", "ApiPending"):
            return None  # still working: find_by_ref should have found it
        state = (
            "rejected"
            if status == "Inactive" and not filled
            else ("filled" if status == "Filled" or (filled and not trades) else "cancelled")
        )
        message = f"recovered from IBKR ({'executions' if fills else 'completed orders'})"
        return BrokerOrderStatus(state, int(filled), avg, message)  # type: ignore[arg-type]

    def reset(self) -> None:
        self._trades.clear()

    def positions(self) -> dict[int, float]:
        return {
            p.contract.conId: float(p.position)
            for p in self.ib.positions(self.conn.account)
            if p.contract.secType == "OPT" and p.position
        }


def _num(value, signed: bool = False) -> float | None:
    """IB uses NaN / -1 for 'no data'; map those to None."""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or (number < 0 and not signed) else number
