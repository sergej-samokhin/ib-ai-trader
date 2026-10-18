"""IBKRBroker.lookup_final against stubbed ib_async objects (no gateway needed)."""

from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from ibtrader.ibkr import IBKRBroker

REF = "momentum-agent|nvda-momo-001|v1|int_1"


class _IB:
    def __init__(self, trades, fills):
        self._trades, self._fills = trades, fills

    def trades(self):
        return self._trades

    def fills(self):
        return self._fills


def broker(trades, fills) -> IBKRBroker:
    b = IBKRBroker.__new__(IBKRBroker)  # skip the connection
    b.ib = _IB(trades, fills)
    b._trades = {}
    return b


def trade(status, filled=0, ref=REF):
    return NS(
        order=NS(orderRef=ref, filledQuantity=filled), orderStatus=NS(status=status, filled=filled)
    )


def fill(sec_type, con_id, shares, price, ref=REF):
    return NS(
        contract=NS(secType=sec_type, conId=con_id),
        execution=NS(orderRef=ref, shares=shares, price=price),
    )


def test_no_record_returns_none():
    assert broker([trade("Filled", 1, ref="other")], []).lookup_final(REF) is None


def test_spread_uses_bag_level_net_price():
    fills = [fill("BAG", 0, 1, 2.4), fill("BAG", 0, 1, 2.6), fill("OPT", 1, 2, 4.0)]
    status = broker([trade("Filled", 2)], fills).lookup_final(REF)
    assert (status.state, status.filled_qty) == ("filled", 2)
    assert status.avg_fill_price == pytest.approx(2.5)


def test_single_leg_fill():
    status = broker([trade("Filled", 1)], [fill("OPT", 7, 1, 3.1)]).lookup_final(REF)
    assert (status.state, status.filled_qty, status.avg_fill_price) == ("filled", 1, 3.1)


def test_spread_with_only_leg_executions_is_ambiguous():
    fills = [fill("OPT", 1, 2, 4.0), fill("OPT", 2, 2, 1.5)]
    assert broker([trade("Filled", 2)], fills).lookup_final(REF) is None


def test_reported_fill_without_executions_stays_unknown():
    assert broker([trade("Cancelled", 1)], []).lookup_final(REF) is None


@pytest.mark.parametrize(
    ("ib_status", "state"),
    [("Cancelled", "cancelled"), ("ApiCancelled", "cancelled"), ("Inactive", "rejected")],
)
def test_unfilled_terminal_states(ib_status, state):
    status = broker([trade(ib_status)], []).lookup_final(REF)
    assert (status.state, status.filled_qty, status.avg_fill_price) == (state, 0, None)


@pytest.mark.parametrize("ib_status", ["Submitted", "PreSubmitted", "PendingCancel"])
def test_still_working_returns_none(ib_status):
    assert broker([trade(ib_status)], []).lookup_final(REF) is None
