"""
Kalshi API client wrapper using pykalshi.

Manages authentication, websocket feed, price cache, and order execution.

Required environment variables:
    KALSHI_{DEMO,PROD}_API_KEY_ID   - API key IDs for each environment
    KALSHI_{DEMO,PROD}_KEY_B64      - base64-encoded RSA private key PEMs
                                      (Railway/prod). Locally, drop the PEMs at
                                      backend/kalshi-{demo,prod}-key.pem instead.
    KALSHI_ENV                      - "prod" for production, otherwise demo
"""

import base64
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logger = logging.getLogger(__name__)


def _decode_pem(env_var: str, fallback_filename: str) -> str | None:
    """Decode a base64-encoded PEM from env var into a temp file, or fall
    back to a local file alongside backend/.

    Lets the same code path work locally (reads the PEM off disk) and on
    Railway/other hosts where the private key arrives as a base64 env var
    because filesystem writes aren't available at deploy time.
    """
    b64 = os.getenv(env_var)
    if b64:
        try:
            pem_bytes = base64.b64decode(b64)
        except Exception as e:
            logger.error(f"Failed to base64-decode {env_var}: {e}")
            return None
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pem", mode="wb")
        tmp.write(pem_bytes)
        tmp.close()
        return tmp.name

    fallback_path = Path(__file__).parent.parent / fallback_filename
    if fallback_path.exists():
        return str(fallback_path)
    return None


# Resolve private key paths once at import time. Either resolves to a real
# file on disk (local dev) or to a temp file decoded from the env var (prod).
demo_pem_path = _decode_pem("KALSHI_DEMO_KEY_B64", "kalshi-demo-key.pem")
prod_pem_path = _decode_pem("KALSHI_PROD_KEY_B64", "kalshi-prod-key.pem")

from pykalshi import (
    KalshiClient,
    MarketStatus,
    OrderbookManager,
    OrderbookSnapshotMessage,
    OrderbookDeltaMessage,
    OrderStatus,
    RateLimiter,
    Action,
    Side,
    TimeInForce,
)


def _fill_complementary_side(prices: dict) -> dict:
    """YES + NO = $1.00 by construction on Kalshi. When one side is missing
    (common on thin books — we get YES quotes over the ticker feed but no
    NO quotes), derive it from the other side so NO-side EV checks aren't
    starved of data.
    """
    yb, ya = prices.get("yes_bid", 0), prices.get("yes_ask", 0)
    nb, na = prices.get("no_bid", 0), prices.get("no_ask", 0)
    if (not nb) and ya:
        prices["no_bid"] = round(1.0 - ya, 2)
    if (not na) and yb:
        prices["no_ask"] = round(1.0 - yb, 2)
    if (not yb) and na:
        prices["yes_bid"] = round(1.0 - na, 2)
    if (not ya) and nb:
        prices["yes_ask"] = round(1.0 - nb, 2)
    return prices


class KalshiManager:
    """Manages Kalshi API connectivity, websocket feed, and price cache.

    Maintains two clients:
      - client: primary (follows KALSHI_ENV, typically prod — has live MLB markets)
      - demo_client: secondary (demo account, used for dry-run balance/positions)
    """

    def __init__(self):
        self.client: KalshiClient | None = None
        self.demo_client: KalshiClient | None = None
        self.feed = None
        self._price_cache: dict[str, dict] = {}
        self._books: dict[str, OrderbookManager] = {}
        self._subscribed: set[str] = set()
        self._lock = threading.Lock()
        self._tick_callbacks: list[Callable] = []
        self._fill_callbacks: list[Callable] = []
        # Liveness clock per ticker — last time we heard ANY price update on
        # this market. Used by `is_market_stale` to gate trades against dead
        # markets (the no_market_data losing-game pattern from the backtest).
        self._last_tick_ts: dict[str, float] = {}
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected and self.client is not None

    def connect(self):
        """Initialize Kalshi client and start websocket feed."""
        env = os.getenv("KALSHI_ENV", "demo").lower()
        demo = env != "prod"

        api_key_id = os.getenv("KALSHI_DEMO_API_KEY_ID" if demo else "KALSHI_PROD_API_KEY_ID")
        private_key_path = demo_pem_path if demo else prod_pem_path

        if not api_key_id or not private_key_path:
            prefix = "KALSHI_DEMO" if demo else "KALSHI_PROD"
            raise ValueError(
                f"Kalshi {env} credentials not configured. "
                f"Set {prefix}_API_KEY_ID and either {prefix}_KEY_B64 or "
                f"place the PEM at backend/kalshi-{'demo' if demo else 'prod'}-key.pem"
            )

        self.client = KalshiClient(
            api_key_id=api_key_id,
            private_key_path=private_key_path,
            demo=demo,
            rate_limiter=RateLimiter(requests_per_second=8.0),
        )

        # On the REAL-money client, wrap .post so every /portfolio/orders
        # call logs the raw response dict before pydantic parses it (and
        # before `extra="ignore"` drops any unknown fields). This is how
        # we caught the lost-trade bug: the POST body has fill_count_fp;
        # the follow-up GET 404s. The raw log is the source of truth.
        if not demo:
            _orig_post = self.client.post

            def _logging_post(endpoint: str, data):
                resp = _orig_post(endpoint, data)
                if endpoint.startswith("/portfolio/orders") and isinstance(resp, dict):
                    # At INFO while we validate fill-cost semantics
                    # (per-contract vs. total). Can drop back to DEBUG
                    # once _actual_fill is confirmed correct.
                    try:
                        logger.info(f"[kalshi] POST {endpoint} body: {resp}")
                    except Exception:
                        pass
                return resp

            self.client.post = _logging_post  # type: ignore[method-assign]

        self.feed = self.client.feed()
        self.feed.on("ticker", self._on_ticker)
        self.feed.on("orderbook_delta", self._on_orderbook)
        # Private user-scoped channel: instant notification when our own
        # orders fill. Drives real-time UI state (see trader.py
        # dispatch_websocket_fill) instead of waiting on the clean-window
        # timer or tick-based polling.
        self.feed.on("fill", self._on_fill)
        self.feed.start()
        try:
            # No market_ticker — "fill" is a private per-user channel.
            self.feed.subscribe("fill")
            logger.info("Subscribed to private fill channel")
        except Exception as e:
            # Don't hard-fail on connect if the fill channel isn't
            # available; the timer / tick fallbacks still resolve trades.
            logger.warning(f"Failed to subscribe to fill channel: {e}")
        self._connected = True

        env_label = "demo" if demo else "PRODUCTION"
        logger.info(f"Kalshi connected ({env_label})")

        # Also connect a demo client if we're in prod mode, for dry-run queries.
        # If demo credentials aren't configured, silently skip.
        if not demo:
            demo_key = os.getenv("KALSHI_DEMO_API_KEY_ID")
            if demo_key and demo_pem_path:
                try:
                    self.demo_client = KalshiClient(
                        api_key_id=demo_key,
                        private_key_path=demo_pem_path,
                        demo=True,
                        rate_limiter=RateLimiter(requests_per_second=8.0),
                    )
                    logger.info("Kalshi demo client connected (for dry-run balance)")
                except Exception as e:
                    logger.warning(f"Demo client failed to connect: {e}")
        else:
            # Primary IS demo, so demo_client aliases to it
            self.demo_client = self.client

    def disconnect(self):
        """Clean shutdown of feed and client."""
        if self.feed:
            try:
                self.feed.stop()
            except Exception:
                pass
        self._connected = False
        logger.info("Kalshi disconnected")

    # ------------------------------------------------------------------
    # Price cache & websocket
    # ------------------------------------------------------------------

    def on_tick(self, callback: Callable[[str, dict], None]):
        """Register a callback for price updates: fn(market_ticker, prices_dict)."""
        self._tick_callbacks.append(callback)

    def on_fill(self, callback: Callable[[object], None]):
        """Register a callback for private fill events: fn(FillMessage).

        Callbacks fire on the feed thread — same thread as on_tick. Each
        callback is wrapped in try/except so one bad handler can't break
        the others (or the feed listener).
        """
        self._fill_callbacks.append(callback)

    def _on_ticker(self, msg):
        """Handle incoming websocket ticker messages."""
        ticker = msg.market_ticker
        prices = _fill_complementary_side({
            "yes_bid": float(getattr(msg, "yes_bid_dollars", None) or 0),
            "yes_ask": float(getattr(msg, "yes_ask_dollars", None) or 0),
            "no_bid": float(getattr(msg, "no_bid_dollars", None) or 0),
            "no_ask": float(getattr(msg, "no_ask_dollars", None) or 0),
        })
        with self._lock:
            self._price_cache[ticker] = prices
            self._last_tick_ts[ticker] = time.time()

        for cb in self._tick_callbacks:
            try:
                cb(ticker, prices)
            except Exception as e:
                logger.error(f"Tick callback error: {e}")

    def _on_fill(self, msg):
        """Handle incoming private fill messages (our own orders)."""
        for cb in self._fill_callbacks:
            try:
                cb(msg)
            except Exception as e:
                logger.error(f"Fill callback error: {e}")

    def _on_orderbook(self, msg):
        """Handle orderbook snapshot and delta messages."""
        try:
            if isinstance(msg, OrderbookSnapshotMessage):
                ticker = msg.market_ticker
                has_depth = bool(msg.yes_dollars or msg.no_dollars)
                if has_depth:
                    logger.info(f"Orderbook snapshot: {ticker} (live)")
                    with self._lock:
                        book = self._books.get(ticker)
                        if book is None:
                            book = OrderbookManager(ticker)
                            self._books[ticker] = book
                        book.apply_snapshot(msg.yes_dollars, msg.no_dollars)
            elif isinstance(msg, OrderbookDeltaMessage):
                ticker = msg.market_ticker
                with self._lock:
                    book = self._books.get(ticker)
                    if book:
                        book.apply_delta(msg.side, msg.price_dollars, msg.delta_fp)
        except Exception as e:
            logger.debug(f"Orderbook message error: {e}")

    def subscribe(self, market_ticker: str):
        """Subscribe to ticker price updates for a market.

        NOTE: orderbook_delta is intentionally NOT subscribed — Kalshi's
        websocket crashes on MLB orderbook messages (empty snapshots,
        ValueError on deltas).  REST fallback handles orderbook reads
        for position sizing.
        """
        if not self.feed or market_ticker in self._subscribed:
            return
        self.feed.subscribe("ticker", market_ticker=market_ticker)
        self._subscribed.add(market_ticker)
        # Seed the liveness clock so `is_market_stale` doesn't block trades
        # on a freshly-subscribed ticker before the first real tick lands.
        # Real ticks overwrite this in _on_ticker; the seed just buys the
        # market a full `stale_market_seconds` warm-up window.
        with self._lock:
            self._last_tick_ts[market_ticker] = time.time()
        logger.info(f"Subscribed to {market_ticker}")

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    def get_markets(self, event_ticker: str) -> list[dict]:
        """Get all open markets for a Kalshi event."""
        if not self.client:
            return []

        try:
            markets = self.client.get_markets(
                event_ticker=event_ticker,
                status=MarketStatus.OPEN,
            )
        except Exception as e:
            logger.error(f"Failed to fetch markets for {event_ticker}: {e}")
            return []

        result = []
        for m in markets:
            row = {
                "ticker": m.ticker,
                "event_ticker": getattr(m, "event_ticker", event_ticker),
                "title": getattr(m, "title", ""),
                "subtitle": getattr(m, "subtitle", ""),
                "yes_bid": float(getattr(m, "yes_bid_dollars", None) or 0),
                "yes_ask": float(getattr(m, "yes_ask_dollars", None) or 0),
                "no_bid": float(getattr(m, "no_bid_dollars", None) or 0),
                "no_ask": float(getattr(m, "no_ask_dollars", None) or 0),
                "volume": str(getattr(m, "volume_fp", "0")),
                "status": str(getattr(m, "status", "")).split(".")[-1].lower(),
            }
            _fill_complementary_side(row)
            result.append(row)
        return result

    def get_orderbook_snapshot(
        self, ticker: str, side: str, levels: int = 5,
    ) -> dict:
        """Read top-N bid/ask levels from the local websocket-maintained
        orderbook, in the trade's side-units.

        Bids = resting orders you'd sell into at exit. YES trades read
        `book.yes` directly; NO trades read `book.no`. Sorted desc.

        Asks = resting orders you cross to enter. Opposite-side bids
        flipped into traded-side units (`1 - p`). Same convention
        `_get_ask_levels` and `has_exit_liquidity` use, so a snapshot
        logged here matches what sizing / exit-liquidity checks saw.

        Returns empty lists + zero depths when the local book hasn't
        been populated yet (fresh subscription, pre-first-snapshot).
        Caller decides whether to skip the write.
        """
        with self._lock:
            book = self._books.get(ticker)
        if book is None:
            return {"bids": [], "asks": [], "total_bid_depth": 0, "total_ask_depth": 0}

        same_side = book.yes if side == "YES" else book.no
        opp_side = book.no if side == "YES" else book.yes

        def _parse(d):
            out = []
            for p_str, q_str in d.items():
                try:
                    p = float(p_str)
                    q = int(float(q_str))
                except (TypeError, ValueError):
                    continue
                if q > 0:
                    out.append((p, q))
            return out

        bid_raw = _parse(same_side)
        ask_raw = [(round(1.0 - p, 4), q) for p, q in _parse(opp_side)]

        bid_sorted = sorted(bid_raw, key=lambda x: x[0], reverse=True)
        ask_sorted = sorted(ask_raw, key=lambda x: x[0])

        return {
            "bids": bid_sorted[:levels],
            "asks": ask_sorted[:levels],
            "total_bid_depth": sum(q for _, q in bid_raw),
            "total_ask_depth": sum(q for _, q in ask_raw),
        }

    def has_exit_liquidity(
        self,
        ticker: str,
        side: str,
        entry_price: float,
        quantity: int,
        max_slippage_dollars: float = 0.05,
    ) -> bool:
        """True iff there are enough resting bids on the trade's side to
        absorb an IOC exit of `quantity` contracts within
        `max_slippage_dollars` of the entry price.

        The worst-case exit happens when the limit sell at target doesn't
        fill and the trader IOCs into the bid book at clean-window expiry.
        If we can't exit the full position within `max_slippage`, the trade
        is structurally doomed — entry taker fee paid, then a fire-sale
        below entry. Skip it before placing the buy.

        Reads the local OrderbookManager (websocket-maintained, microseconds,
        no API call). Bid prices in `book.yes` / `book.no` are already in
        their respective side-units, so the comparison against `entry_price`
        is direct (entry_price comes from vwap which is also in side units).

        Returns True when the local book is unavailable — we don't want to
        block trades just because the orderbook hasn't snapshotted yet.
        Pair this with `is_market_stale` as the cheap first filter.
        """
        if quantity <= 0:
            return True
        with self._lock:
            book = self._books.get(ticker)
        if book is None:
            return True  # no local depth; let the trade through
        bids = book.yes if side == "YES" else book.no
        if not bids:
            return False
        floor = entry_price - max_slippage_dollars
        available = 0
        for price_str, qty_str in bids.items():
            try:
                price = float(price_str)
                qty = int(float(qty_str))
            except (TypeError, ValueError):
                continue
            if qty <= 0 or price < floor:
                continue
            available += qty
            if available >= quantity:
                return True
        return False

    def is_market_stale(self, ticker: str, max_age_seconds: float = 60) -> bool:
        """True if no websocket tick has arrived for `ticker` in the last
        `max_age_seconds`. Used by the picker to skip dead markets — the
        backtest's losing-game audit (LAA@CIN −$484, PHI@SF −$196,
        ATL@LAA −$104) found 24–50% of trades on those days landed on
        markets with zero exit-window activity, paying entry taker fees
        for nothing.

        A ticker that's never been seen is treated as stale (we have no
        evidence it's alive). This is conservative: a freshly-subscribed
        market may briefly read stale, but the first tick clears it.
        """
        with self._lock:
            last = self._last_tick_ts.get(ticker)
        if last is None:
            return True
        return (time.time() - last) > max_age_seconds

    def get_prices(self, market_ticker: str) -> dict:
        """Get current prices from cache, falling back to REST API."""
        with self._lock:
            cached = self._price_cache.get(market_ticker)
        if cached:
            return cached

        if not self.client:
            return {"yes_bid": 0, "yes_ask": 0, "no_bid": 0, "no_ask": 0}

        try:
            market = self.client.get_market(market_ticker)
            prices = _fill_complementary_side({
                "yes_bid": float(getattr(market, "yes_bid_dollars", None) or 0),
                "yes_ask": float(getattr(market, "yes_ask_dollars", None) or 0),
                "no_bid": float(getattr(market, "no_bid_dollars", None) or 0),
                "no_ask": float(getattr(market, "no_ask_dollars", None) or 0),
            })
        except Exception as e:
            logger.error(f"Failed to fetch prices for {market_ticker}: {e}")
            return {"yes_bid": 0, "yes_ask": 0, "no_bid": 0, "no_ask": 0}

        with self._lock:
            self._price_cache[market_ticker] = prices
        return prices

    # ------------------------------------------------------------------
    # Position sizing
    # ------------------------------------------------------------------

    def _get_ask_levels(self, market_ticker: str, side: str) -> list[tuple[float, int]]:
        """
        Get ask levels for a side. Reads from local OrderbookManager
        (microseconds, no network) if available, else falls back to REST.
        """
        with self._lock:
            book = self._books.get(market_ticker)

        if book and book.best_ask is not None:
            # Local book: read the opposite side's dict (NO bids → YES asks)
            if side == "YES":
                raw = dict(book.no)
            else:
                raw = dict(book.yes)

            levels = []
            for price_str, qty_str in raw.items():
                ask_price = round(1.0 - float(price_str), 4)
                qty = int(float(qty_str))
                if qty > 0:
                    levels.append((ask_price, qty))
            levels.sort()
            if levels:
                return levels

        # Fallback: REST API (local book empty or not subscribed)
        if not self.client:
            return []

        market = self.client.get_market(market_ticker)
        rest_book = market.get_orderbook(depth=50)
        ob = rest_book.model_dump().get("orderbook", {})

        if side == "YES":
            raw_levels = ob.get("no_dollars", [])
        else:
            raw_levels = ob.get("yes_dollars", [])

        levels = []
        for price_str, qty_str in reversed(raw_levels):
            ask_price = round(1.0 - float(price_str), 4)
            qty = int(float(qty_str))
            if qty > 0:
                levels.append((ask_price, qty))
        return levels

    def calculate_position_size(
        self,
        market_ticker: str,
        side: str,
        max_dollars: float = 500.0,
        max_slippage_cents: float = 1.0,
    ) -> dict:
        """
        Calculate position size from orderbook depth.

        Reads from the local websocket-maintained book (zero latency)
        when available, falls back to REST API otherwise.

        Two constraints:
          1. Slippage: never pay more than max_slippage_cents above best ask
          2. Budget: never spend more than max_dollars total

        Returns:
            {
                "contracts": int,
                "best_ask": float,
                "vwap": float,
                "slippage": float,
                "total_cost": float,
                "depth_at_limit": int,
                "budget_limited": bool,
                "source": "local" | "rest",
            }
        """
        empty = {"contracts": 0, "best_ask": 0, "vwap": 0, "slippage": 0,
                 "total_cost": 0, "depth_at_limit": 0, "budget_limited": False,
                 "source": "none"}

        with self._lock:
            book = self._books.get(market_ticker)
            has_local = book is not None and book.best_ask is not None

        ask_levels = self._get_ask_levels(market_ticker, side)
        source = "local" if has_local else "rest"

        if not ask_levels:
            empty["source"] = source
            return empty

        best_ask = ask_levels[0][0]
        slippage_limit = best_ask + max_slippage_cents / 100

        depth_at_limit = 0
        for ask_price, qty in ask_levels:
            if ask_price > slippage_limit:
                break
            depth_at_limit += qty

        contracts = 0
        total_cost = 0.0
        budget_limited = False

        for ask_price, qty in ask_levels:
            if ask_price > slippage_limit:
                break
            can_afford = int((max_dollars - total_cost) / ask_price) if ask_price > 0 else 0
            take = min(qty, can_afford)
            if take <= 0:
                budget_limited = True
                break
            contracts += take
            total_cost += ask_price * take
            if total_cost >= max_dollars - 0.01:
                budget_limited = True
                break

        vwap = round(total_cost / contracts, 4) if contracts > 0 else best_ask
        slippage = round(vwap - best_ask, 4) if contracts > 0 else 0

        return {
            "contracts": contracts,
            "best_ask": best_ask,
            "vwap": vwap,
            "slippage": slippage,
            "total_cost": round(total_cost, 2),
            "depth_at_limit": depth_at_limit,
            "budget_limited": budget_limited,
            "source": source,
        }

    # ------------------------------------------------------------------
    # Order execution
    # ------------------------------------------------------------------

    def place_order(
        self,
        market_ticker: str,
        action: str,
        side: str,
        quantity: int,
        price: float | None = None,
        time_in_force: str = "gtc",
        use_demo: bool = False,
        client_order_id: str | None = None,
    ) -> dict:
        """
        Place an order on Kalshi.

        Args:
            action: "buy" or "sell"
            side: "YES" or "NO"
            quantity: number of contracts
            price: limit price in dollars (e.g. 0.45). None for market-like orders.
            time_in_force: "gtc", "ioc", or "fok"
            use_demo: route to demo account (for dry-run mode)
            client_order_id: optional idempotency key — LiveLine tags every
                order with "liveline-<trade_id[:8]>" so startup cleanup can
                distinguish our orders from other Kalshi activity on the
                same account and only cancel ours.
        """
        client = self._account_client(use_demo)
        if not client:
            raise RuntimeError(f"Kalshi {'demo' if use_demo else 'prod'} client not connected")

        _action = Action.BUY if action == "buy" else Action.SELL
        _side = Side.YES if side == "YES" else Side.NO
        _tif = {
            "gtc": TimeInForce.GTC,
            "ioc": TimeInForce.IOC,
            "fok": TimeInForce.FOK,
        }.get(time_in_force, TimeInForce.GTC)

        kwargs: dict = {
            "ticker": market_ticker,
            "action": _action,
            "side": _side,
            "count_fp": f"{quantity:.2f}",
            "time_in_force": _tif,
        }
        if client_order_id is not None:
            kwargs["client_order_id"] = client_order_id
        if price is not None:
            if side == "YES":
                kwargs["yes_price_dollars"] = f"{price:.2f}"
            else:
                kwargs["no_price_dollars"] = f"{price:.2f}"

        order = client.portfolio.place_order(**kwargs)
        # Raw POST body is already logged by the client-level hook in
        # connect(); no need to re-dump the parsed object here.
        return {
            "order_id": getattr(order, "order_id", None),
            "status": str(getattr(order, "status", "unknown")).split(".")[-1].lower(),
            "ticker": market_ticker,
            "action": action,
            "side": side,
            "quantity": quantity,
            "price": price,
            # Pass through any fill info the place_order response may contain.
            # Some APIs return fill data on the initial order response for IOC
            # orders (avoiding the need for a follow-up get_order call).
            "fill_count_fp": getattr(order, "fill_count_fp", None),
            "filled_count": getattr(order, "filled_count", None),
            "quantity_filled": getattr(order, "quantity_filled", None),
            "remaining_count_fp": getattr(order, "remaining_count_fp", None),
            # Taker / maker fill cost in dollars. Dividing by fill_count
            # gives the actual VWAP per contract — the only correct way
            # to compute P&L on an IOC exit (the limit price is $0.01,
            # but the real fill lands at whatever the bid was).
            "taker_fill_cost_dollars": getattr(order, "taker_fill_cost_dollars", None),
            "maker_fill_cost_dollars": getattr(order, "maker_fill_cost_dollars", None),
        }

    def batch_place_orders(self, orders: list[dict]) -> list[dict]:
        """
        Place multiple orders atomically in a single API call.

        Each dict in orders should have: ticker, action, side, count_fp,
        and a price key (yes_price_dollars or no_price_dollars).
        """
        if not self.client:
            raise RuntimeError("Kalshi not connected")

        results = self.client.portfolio.batch_place_orders(orders)
        return [
            {
                "order_id": getattr(o, "order_id", None),
                "status": str(getattr(o, "status", "unknown")).split(".")[-1].lower(),
            }
            for o in results
        ]

    def cancel_order(self, order_id: str, use_demo: bool = False) -> dict:
        """Cancel a resting order on the selected account."""
        client = self._account_client(use_demo)
        if not client:
            raise RuntimeError(f"Kalshi {'demo' if use_demo else 'prod'} client not connected")
        order = client.portfolio.cancel_order(order_id)
        return {
            "order_id": order_id,
            "status": str(getattr(order, "status", "unknown")).split(".")[-1].lower(),
        }

    def get_order(self, order_id: str, use_demo: bool = False):
        """Fetch an order by id from the selected account.

        Returns None if the order no longer exists (404) — this happens
        naturally when IOC orders don't fill and get removed.
        """
        client = self._account_client(use_demo)
        if not client:
            return None
        try:
            return client.portfolio.get_order(order_id)
        except Exception as e:
            msg = str(e).lower()
            if "not_found" in msg or "404" in msg:
                return None
            raise

    def get_order_fill_vwap(
        self, order_id: str, side: str, use_demo: bool = False,
    ) -> tuple[float, int] | None:
        """Return (per_contract_vwap, total_qty) for every fill on `order_id`,
        in `side`-units (YES price for YES, 1.0 - yes_price for NO).

        Authoritative source for exit-price logging when a limit sell has
        already filled: Kalshi aggregates partial fills server-side and
        echoes per-fill `yes_price_dollars` / `count_fp`, so a VWAP here
        captures both maker price improvement and multi-level partials.

        Returns None on lookup failure or zero fills — caller should fall
        back to the intent price (sell_target) and surface a warning."""
        client = self._account_client(use_demo)
        if not client:
            return None
        try:
            fills = client.portfolio.get_fills(order_id=order_id, fetch_all=True)
        except Exception as e:
            logger.error(f"get_order_fill_vwap({order_id}): {e}")
            return None
        total_cost = 0.0
        total_qty = 0
        for f in fills:
            try:
                qty = int(float(getattr(f, "count_fp", 0) or 0))
                yes_price = float(getattr(f, "yes_price_dollars", 0) or 0)
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            per_contract = yes_price if side == "YES" else 1.0 - yes_price
            total_cost += per_contract * qty
            total_qty += qty
        if total_qty == 0:
            return None
        vwap = round(total_cost / total_qty, 4)
        return (vwap, total_qty)

    def _account_client(self, use_demo: bool) -> KalshiClient | None:
        """Pick which client to use for account queries (balance/positions)."""
        if use_demo and self.demo_client is not None:
            return self.demo_client
        return self.client

    def get_balance(self, use_demo: bool = False) -> dict:
        """
        Get account balance.

        use_demo=True routes to the demo account (for dry-run display).
        Falls back to primary client if demo client isn't configured.
        """
        client = self._account_client(use_demo)
        empty = {
            "balance_cents": 0, "portfolio_value_cents": 0,
            "balance": 0.0, "portfolio_value": 0.0, "total": 0.0,
            "account": "demo" if use_demo else "prod",
        }
        if not client:
            return empty
        try:
            bal = client.portfolio.get_balance()
            balance_cents = int(getattr(bal, "balance", 0) or 0)
            pv_cents = int(getattr(bal, "portfolio_value", 0) or 0)
            return {
                "balance_cents": balance_cents,
                "portfolio_value_cents": pv_cents,
                "balance": round(balance_cents / 100, 2),
                "portfolio_value": round(pv_cents / 100, 2),
                "total": round((balance_cents + pv_cents) / 100, 2),
                "account": "demo" if use_demo else "prod",
            }
        except Exception as e:
            logger.error(f"Failed to fetch balance ({'demo' if use_demo else 'prod'}): {e}")
            return empty

    def get_open_orders(self, use_demo: bool = False) -> list[dict]:
        """Fetch every resting order on the selected account.

        Used by the startup cleanup to find orphaned LiveLine orders
        (client_order_id starts with `liveline-`). Returns dicts keyed
        to what cleanup actually needs — the pykalshi Order object
        doesn't expose client_order_id as a property, so we reach into
        `order.data` defensively."""
        client = self._account_client(use_demo)
        if not client:
            return []
        try:
            raw = list(client.portfolio.get_orders(
                status=OrderStatus.RESTING,
                fetch_all=True,
            ))
        except Exception as e:
            logger.error(f"Failed to fetch open orders: {e}")
            return []

        out: list[dict] = []
        for o in raw:
            # client_order_id lives on the pydantic model, not the wrapper.
            coid = None
            try:
                coid = getattr(o, "client_order_id", None)
            except Exception:
                pass
            if coid is None:
                data = getattr(o, "data", None)
                if data is not None:
                    coid = getattr(data, "client_order_id", None)
            out.append({
                "order_id": getattr(o, "order_id", None),
                "ticker": getattr(o, "ticker", None),
                "status": str(getattr(o, "status", "unknown")).split(".")[-1].lower(),
                "client_order_id": coid,
            })
        return out

    def get_live_positions(self, use_demo: bool = False) -> list[dict]:
        """Fetch current open positions from Kalshi (demo or prod)."""
        client = self._account_client(use_demo)
        if not client:
            return []
        try:
            raw = list(client.portfolio.get_positions(fetch_all=True))
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            return []

        positions = []
        for p in raw:
            ticker = getattr(p, "ticker", "")
            qty = int(getattr(p, "position", 0) or 0)
            if qty == 0:
                continue
            positions.append({
                "market_ticker": ticker,
                "quantity": qty,
                "market_exposure_cents": int(getattr(p, "market_exposure", 0) or 0),
                "realized_pnl_cents": int(getattr(p, "realized_pnl", 0) or 0),
                "unrealized_pnl_cents": int(getattr(p, "unrealized_pnl", 0) or 0),
                "total_traded_cents": int(getattr(p, "total_traded", 0) or 0),
                "resting_orders_count": int(getattr(p, "resting_orders_count", 0) or 0),
                "fees_paid_cents": int(getattr(p, "fees_paid", 0) or 0),
            })
        return positions


# Module-level singleton
kalshi = KalshiManager()
