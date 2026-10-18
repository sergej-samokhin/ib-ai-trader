"""WSB radar: producer-side attention tracking for r/wallstreetbets (strategy phase 0).

This package collects a bounded sample of posts and comments, extracts tickers, scores a
rule-based sentiment and logs daily attention features (strategy draft, sections 3.2 and 3.3).
It never places orders and never writes signals: nothing from Reddit reaches the trading core.
The trading modules must not import from here, and this package imports nothing that trades
(enforced by ``tests/test_radar_isolation.py``).
"""
