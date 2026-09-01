import asyncio
import aiohttp
from typing import Dict, Any, List

class MarketDataService:
    """
    Real-time market data service for Nado DEX (Ink L2 by Kraken).
    Retrieves live price tickers, multi-timeframe candlesticks (15m, 1H, 4H), order book depth,
    technical indicators (RSI, EMA, MACD), and sentiment (Crypto Fear & Greed Index).
    """
    def __init__(self, exchange_name: str = "Nado_DEX (Ink L2)", logger=None, nado_client=None):
        self.exchange_name = exchange_name
        self.logger = logger
        self.session = None
        import os
        self._oi_file = "data/memory/oi_history.json"
        self._oi_history = self._load_oi_history()

        from core.config import config
        self.is_nado = config.NADO_LIVE_TRADING_ENABLED or config.TRADING_ENGINE == "NADO"
        self.nado_client = nado_client
        self.product_map = {}
        if self.is_nado:
            if self.nado_client:
                # Use the provided global Nado Client
                self._load_nado_product_map()
            else:
                # Fallback to instantiating if none was provided
                try:
                    from core.nado_helper import create_configured_nado_client
                    private_key = os.getenv("INK_PRIVATE_KEY")
                    self.nado_client = create_configured_nado_client(
                        network_name=config.NADO_NETWORK,
                        signer=private_key
                    )
                    self._load_nado_product_map()
                except Exception as e:
                    self._log(f"⚠️ [MarketDataService] Failed to init Nado Client: {e}")

    def _load_nado_product_map(self):
        try:
            products = self.nado_client.market.get_all_product_symbols()
            markets = self.nado_client.market.get_all_engine_markets()
            perp_ids = {m.product_id for m in markets.perp_products}
            
            for p in products:
                if p.product_id in perp_ids:
                    base_symbol = p.symbol.split('-')[0].upper()
                    self.product_map[base_symbol] = p.product_id
        except Exception as e:
            self._log(f"⚠️ [MarketDataService] Failed to load Nado product map: {e}")

    def _load_oi_history(self) -> dict:
        from core.state_store import StateStore
        data = StateStore.load(self._oi_file)
        if data:
            return data
        return {}

    def _save_oi_history(self):
        from core.state_store import StateStore
        StateStore.save(self._oi_file, self._oi_history)

    def _log(self, msg: str, level: str = "error"):
        if self.logger:
            if level == "error": self.logger.error(msg)
            elif level == "warning": self.logger.warning(msg)
            else: self.logger.info(msg)
        else:
            print(f"[{level.upper()}] {msg}")

    async def _get_session(self):
        from core.session import SessionManager
        return await SessionManager.get()

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def fetch_active_perps(self, limit: int = 15) -> List[Dict[str, Any]]:
        """
        Dynamically fetches all available perpetual contracts from Nado DEX
        and sorts them by cumulative volume to always trade the most liquid pairs.
        """
        if self.is_nado and self.nado_client:
            try:
                import asyncio
                from nado_protocol.indexer_client.types.query import IndexerMarketSnapshotsParams, IndexerMarketSnapshotInterval
                
                # Fetch a single aggregated market snapshot for all pairs
                params = IndexerMarketSnapshotsParams(interval=IndexerMarketSnapshotInterval(count=1, granularity=86400))
                snapshots = await asyncio.to_thread(self.nado_client.market.get_market_snapshots, params)
                
                if snapshots and snapshots.snapshots:
                    vols = snapshots.snapshots[0].cumulative_volumes
                    products = await asyncio.to_thread(self.nado_client.market.get_all_product_symbols)
                    
                    data = []
                    stablecoins = {'USDT', 'USDC', 'USDE', 'DAI', 'USD'}
                    skip_symbols = {'WBTC'} # Skip WBTC to avoid duplicate BTC exposure
                    
                    for p in products:
                        base_symbol = p.symbol.split('-')[0].upper()
                        if base_symbol in stablecoins or base_symbol in skip_symbols:
                            continue
                        
                        if base_symbol not in self.product_map or self.product_map[base_symbol] != p.product_id:
                            continue
                            
                        vid = str(p.product_id)
                        vol = float(vols.get(vid, 0)) / 1e18
                        data.append((base_symbol, vol))
                        
                    # Sort by highest volume
                    data.sort(key=lambda x: x[1], reverse=True)
                    
                    return [{"symbol": f"{sym}-USD", "volumeQuote": vol} for sym, vol in data[:limit]]
            except Exception as e:
                self._log(f"⚠️ [MarketDataService] Failed to fetch Nado volume snapshots: {e}")
                
            # If dynamic fetch fails, fallback to product_map keys
            stablecoins = {'USDT', 'USDC', 'USDE', 'DAI', 'USD'}
            nado_symbols = [sym for sym in self.product_map.keys() if sym.upper() not in stablecoins]
            return [{"symbol": f"{sym}-USD", "volumeQuote": 10000000} for sym in nado_symbols[:limit]]
            
        # Fallback to Kraken if Nado is disabled
        url = "https://futures.kraken.com/derivatives/api/v3/tickers"
        try:
            session = await self._get_session()
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    tickers = data.get("tickers", [])
                    
                    perps = [t for t in tickers if t.get("tag") == "perpetual" and t.get("pair")]
                    perps.sort(key=lambda x: float(x.get("volumeQuote", 0) or 0), reverse=True)
                    
                    top_symbols = []
                    skip_symbols = {'WBTC'}
                    
                    for p in perps:
                        pair = p.get("pair", "")
                        if ":" in pair:
                            base_asset = pair.split(":")[0]
                            if base_asset == "XBT":
                                base_asset = "BTC"
                            
                            if base_asset in skip_symbols:
                                continue
                                
                            vol = round(float(p.get("volumeQuote", 0)), 2)
                            change = round(float(p.get("change24h", 0)), 2)
                            
                            top_symbols.append({
                                "symbol": base_asset,
                                "vol24h": vol,
                                "change24h": change
                            })
                            if len(top_symbols) >= limit:
                                break
                            
                    if top_symbols:
                        return top_symbols
        except Exception as e:
            self._log(f"⚠️ [MarketDataService] Failed to fetch Kraken tickers: {e}")
            
        # Fallback list if API fails
        return [{"symbol": s, "vol24h": 0, "change24h": 0} for s in ["BTC", "ETH", "SOL", "XRP", "DOGE", "ADA", "LINK", "AVAX"]]

    def _normalize_pair(self, symbol: str) -> str:
        """Helper to convert generic symbols (BTC, SOL) to Kraken Futures Vanilla Perpetual format (PF_XBTUSD)."""
        s = symbol.upper().replace("-", "").replace("/", "").replace("USDC", "").replace("USDT", "")
        if s.startswith("BTC"):
            s = "XBT"
        elif s.startswith("DOGE"):
            s = "XDG"
            
        return f"PF_{s}USD"

    def _normalize_candles(self, raw_candles: List[Any]) -> List[Dict[str, float]]:
        """Unified normalizer that accepts both dict and list API formats and returns a standard dict."""
        normalized = []
        for c in raw_candles:
            if isinstance(c, dict):
                normalized.append({
                    "open": float(c.get("open", 0)),
                    "high": float(c.get("high", 0)),
                    "low": float(c.get("low", 0)),
                    "close": float(c.get("close", 0)),
                    "volume": float(c.get("volume", 0))
                })
            elif isinstance(c, (list, tuple)) and len(c) >= 6:
                # Format: [timestamp, open, high, low, close, volume]
                normalized.append({
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5])
                })
        return normalized


    async def _fetch_nado_candles(self, symbol: str, interval_min: int, limit: int) -> list:
        base_symbol = symbol.split('-')[0].upper()
        if not self.nado_client or base_symbol not in self.product_map:
            return []
        from nado_protocol.indexer_client.types.query import IndexerCandlesticksParams, IndexerCandlesticksGranularity
        import asyncio
        tf_map = {
            15: IndexerCandlesticksGranularity.FIFTEEN_MINUTES,
            60: IndexerCandlesticksGranularity.ONE_HOUR,
            240: IndexerCandlesticksGranularity.FOUR_HOURS,
            1440: IndexerCandlesticksGranularity.ONE_DAY
        }
        params = IndexerCandlesticksParams(
            product_id=self.product_map[base_symbol],
            granularity=tf_map.get(interval_min, IndexerCandlesticksGranularity.FIFTEEN_MINUTES),
            limit=limit
        )
        try:
            candles = await asyncio.to_thread(self.nado_client.market.get_candlesticks, params)
            return [{
                "open": float(c.open_x18) / 1e18,
                "high": float(c.high_x18) / 1e18,
                "low": float(c.low_x18) / 1e18,
                "close": float(c.close_x18) / 1e18,
                "volume": float(c.volume) / 1e18
            } for c in reversed(candles.candlesticks)]
        except Exception as e:
            self._log(f"⚠️ [MarketDataService] Failed to fetch Nado candles for {symbol}: {e}")
            return []

    async def _fetch_ohlc_interval(self, symbol: str, interval_min: int) -> dict:
        pair = self._normalize_pair(symbol)
        
        if getattr(self, "is_nado", False) or True: # Force Nado
            candles = await self._fetch_nado_candles(symbol, interval_min, 20)
            if candles:
                closes = [c["close"] for c in candles]
                current_price = closes[-1]
                price_5_ago = closes[-5] if len(closes) >= 5 else closes[0]
                pct_diff = ((current_price - price_5_ago) / (price_5_ago + 1e-9)) * 100
                trend = "BULLISH" if pct_diff > 0.2 else ("BEARISH" if pct_diff < -0.2 else "NEUTRAL")
                vol = sum(c["volume"] for c in candles)
                return {
                    "interval_min": interval_min,
                    "current_price": round(current_price, 6),
                    "trend": trend,
                    "change_pct": round(pct_diff, 2),
                    "volume": round(vol * current_price, 2),
                    "candles_20": candles
                }
            self._log(f"⚠️ [MarketDataService] Nado candles empty for {symbol}. NO FALLBACK ALLOWED.")
            raise ValueError(f"Nado OHLCV data unavailable for {symbol}")
        
        return {}

    async def fetch_multi_timeframe(self, symbol: str) -> Dict[str, Any]:
        """
        Fetches 15m, 1H (60m), and 4H (240m) trends for Multi-Timeframe Alignment check.
        """
        tf_15m, tf_1h, tf_4h = await asyncio.gather(
            self._fetch_ohlc_interval(symbol, 15),
            self._fetch_ohlc_interval(symbol, 60),
            self._fetch_ohlc_interval(symbol, 240)
        )

        t15 = tf_15m.get("trend", "NEUTRAL") if tf_15m else "NEUTRAL"
        t1h = tf_1h.get("trend", "NEUTRAL") if tf_1h else "NEUTRAL"
        t4h = tf_4h.get("trend", "NEUTRAL") if tf_4h else "NEUTRAL"

        if t15 == t1h == t4h and t15 != "NEUTRAL":
            alignment = "FULL_ALIGNMENT"
        elif t1h == t4h and t15 != t1h:
            alignment = "COUNTER_TREND_WARNING"
        else:
            alignment = "MIXED_CHOP"

        return {
            "trend_15m": t15,
            "trend_1h": t1h,
            "trend_4h": t4h,
            "mtf_alignment": alignment,
            "tf_15m": tf_15m,
            "tf_1h": tf_1h,
            "tf_4h": tf_4h
        }

    async def fetch_ohlcv(self, symbol: str, timeframe: str = "15m") -> Dict[str, Any]:
        """Fetches real candlestick data and computes price trend from Kraken."""
        tf_map = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}
        interval_min = tf_map.get(timeframe.lower(), 15)
        res = await self._fetch_ohlc_interval(symbol, interval_min)
            
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "current_price": res.get("current_price", 0.0),
            "trend": res.get("trend", "neutral").lower(),
            "recent_volume": res.get("volume", 0.0),
            "candles_20": res.get("candles_20", [])
        }

    async def fetch_order_book(self, symbol: str) -> Dict[str, Any]:
        """Fetches real order book depth, spread, and wall strengths from Nado DEX."""
        pair = self._normalize_pair(symbol)
        
        if self.is_nado and self.nado_client:
            try:
                import asyncio
                base_symbol = symbol.split('-')[0].upper()
                product_id = self.product_map.get(base_symbol)
                
                if product_id is not None:
                    # Fetch liquidity up to 20 levels deep
                    liquidity = await asyncio.to_thread(self.nado_client.market.get_market_liquidity, product_id, 20)
                    
                    if liquidity and liquidity.bids and liquidity.asks:
                        # Convert x18 formats
                        bids = [(float(p) / 1e18, float(s) / 1e18) for p, s in liquidity.bids]
                        asks = [(float(p) / 1e18, float(s) / 1e18) for p, s in liquidity.asks]
                        
                        bids = sorted(bids, key=lambda x: x[0], reverse=True)
                        asks = sorted(asks, key=lambda x: x[0])
                        
                        best_bid = bids[0][0]
                        best_ask = asks[0][0]
                        spread = round(best_ask - best_bid, 4)
                        spread_pct = round((spread / best_bid) * 100, 4) if best_bid > 0 else 0.0
                        bid_vol = sum(b[1] for b in bids)
                        ask_vol = sum(a[1] for a in asks)
                        total_vol = bid_vol + ask_vol
                        imbalance = round((bid_vol - ask_vol) / (total_vol + 1e-8), 4)
                        
                        return {
                            "symbol": symbol,
                            "spread": spread,
                            "spread_pct": spread_pct,
                            "bid_volume": round(bid_vol, 2),
                            "ask_volume": round(ask_vol, 2),
                            "imbalance": imbalance,
                            "best_bid": best_bid,
                            "best_ask": best_ask
                        }
            except Exception as e:
                self._log(f"⚠️ [MarketDataService] Failed to fetch Nado order book for {symbol}: {e}. NO FALLBACK ALLOWED.")
                raise ValueError(f"Nado OrderBook data unavailable for {symbol}")
                
        raise ValueError(f"Nado OrderBook data unavailable for {symbol} (Client not initialized)")

    async def fetch_indicators(self, symbol: str) -> Dict[str, Any]:
        """Fetches candle data and computes RSI-14, EMA-20, and MACD."""
        try:
            # We can use the existing _fetch_ohlc_interval to get 15m futures candles
            res = await self._fetch_ohlc_interval(symbol, 15)
            candles = res.get("candles_20", [])
            # We need a longer history to warm up EMA and MACD properly.
            # 200 candles is a good industry standard for stable EMA/MACD values.
            if getattr(self, "is_nado", False):
                candles = await self._fetch_nado_candles(symbol, 15, 200)
            else:
                pair = self._normalize_pair(symbol)
                url = f"https://futures.kraken.com/api/charts/v1/trade/{pair}/15m"
                session = await self._get_session()
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candles = self._normalize_candles(data.get("candles", []))
            
            if True: # Kept to maintain indentation level
                        if candles:
                            closes = [float(c["close"]) for c in candles[-200:]]
                            highs = [float(c["high"]) for c in candles[-200:]]
                            lows = [float(c["low"]) for c in candles[-200:]]
                            if len(closes) >= 35:
                                gains = [max(0, closes[i] - closes[i-1]) for i in range(1, len(closes))]
                                losses = [max(0, closes[i-1] - closes[i]) for i in range(1, len(closes))]
                                
                                # RSI Wilder's Smoothing (RMA)
                                avg_gain = sum(gains[:14]) / 14
                                avg_loss = sum(losses[:14]) / 14
                                for i in range(14, len(gains)):
                                    avg_gain = (avg_gain * 13 + gains[i]) / 14
                                    avg_loss = (avg_loss * 13 + losses[i]) / 14
                                rs = avg_gain / (avg_loss + 1e-6)
                                rsi = round(100 - (100 / (1 + rs)), 2)

                                def calc_ema_series(data, period):
                                    if not data or len(data) < period: return [0] * len(data)
                                    emas = [0] * (period - 1)
                                    ema = sum(data[:period]) / period
                                    emas.append(ema)
                                    k = 2 / (period + 1)
                                    for price in data[period:]:
                                        ema = (price - ema) * k + ema
                                        emas.append(ema)
                                    return emas

                                ema_20_series = calc_ema_series(closes, 20)
                                ema_20 = round(ema_20_series[-1], 6)
                                current_price = closes[-1]
                                ema_trend = "up" if current_price > ema_20 else "down"

                                # MACD (12, 26, 9)
                                ema_12_series = calc_ema_series(closes, 12)
                                ema_26_series = calc_ema_series(closes, 26)
                                macd_line = [ema_12_series[i] - ema_26_series[i] for i in range(len(closes))]
                                
                                # Signal line is EMA(9) of the valid MACD portion
                                macd_valid = macd_line[25:]
                                signal_line_series = calc_ema_series(macd_valid, 9)
                                signal_val = signal_line_series[-1] if signal_line_series else 0
                                
                                macd_val = macd_line[-1]
                                macd_signal_label = "bullish" if macd_val > signal_val else "bearish"

                                # ATR-14 (Wilder's Smoothing)
                                tr_list = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])) for i in range(1, len(closes))]
                                atr_14 = sum(tr_list[:14]) / 14
                                for i in range(14, len(tr_list)):
                                    atr_14 = (atr_14 * 13 + tr_list[i]) / 14
                                atr_pct = round((atr_14 / current_price) * 100, 2)

                                # === ALGORITHMIC SIGNALS (QW #2) ===
                                # These are deterministic, computed in Python — no LLM guessing.

                                # 1. RSI Divergence Detection (last 20 bars)
                                rsi_divergence = "none"
                                # Compute RSI series for divergence check
                                rsi_series = []
                                _ag = sum(gains[:14]) / 14
                                _al = sum(losses[:14]) / 14
                                for i in range(14, len(gains)):
                                    _ag = (_ag * 13 + gains[i]) / 14
                                    _al = (_al * 13 + losses[i]) / 14
                                    _rs = _ag / (_al + 1e-6)
                                    rsi_series.append(round(100 - (100 / (1 + _rs)), 2))
                                
                                if len(rsi_series) >= 20 and len(closes) >= 20:
                                    # Bullish divergence: price makes Lower Low, RSI makes Higher Low
                                    price_tail = closes[-20:]
                                    rsi_tail = rsi_series[-20:]
                                    price_min1_idx = price_tail[:10].index(min(price_tail[:10]))
                                    price_min2_idx = 10 + price_tail[10:].index(min(price_tail[10:]))
                                    if price_tail[price_min2_idx] < price_tail[price_min1_idx] and rsi_tail[price_min2_idx] > rsi_tail[price_min1_idx]:
                                        rsi_divergence = "bullish"
                                    # Bearish divergence: price makes Higher High, RSI makes Lower High
                                    price_max1_idx = price_tail[:10].index(max(price_tail[:10]))
                                    price_max2_idx = 10 + price_tail[10:].index(max(price_tail[10:]))
                                    if price_tail[price_max2_idx] > price_tail[price_max1_idx] and rsi_tail[price_max2_idx] < rsi_tail[price_max1_idx]:
                                        rsi_divergence = "bearish"

                                # 2. MACD Crossover Detection
                                macd_crossover = "none"
                                if len(macd_valid) >= 2 and len(signal_line_series) >= 2:
                                    prev_macd = macd_valid[-2]
                                    prev_signal = signal_line_series[-2]
                                    curr_macd = macd_valid[-1]
                                    curr_signal = signal_line_series[-1]
                                    if prev_macd <= prev_signal and curr_macd > curr_signal:
                                        macd_crossover = "bullish_cross"
                                    elif prev_macd >= prev_signal and curr_macd < curr_signal:
                                        macd_crossover = "bearish_cross"

                                # 3. MACD Histogram momentum
                                macd_histogram = round(macd_val - signal_val, 6)
                                prev_histogram = round(macd_valid[-2] - signal_line_series[-2], 6) if len(macd_valid) >= 2 and len(signal_line_series) >= 2 else 0
                                histogram_momentum = "accelerating" if abs(macd_histogram) > abs(prev_histogram) else "decelerating"

                                # 4. Liquidity Sweep Detection (wick > 60% of candle range on last 5 candles)
                                sweeps_detected = []
                                for ci in range(-5, 0):
                                    if abs(ci) <= len(candles):
                                        c = candles[ci]
                                        o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
                                        rng = h - l
                                        if rng > 0:
                                            upper_wick = h - max(o, cl)
                                            lower_wick = min(o, cl) - l
                                            if upper_wick / rng > 0.6:
                                                sweeps_detected.append({"type": "upper_sweep", "bar": ci, "rejection_from": round(h, 6)})
                                            elif lower_wick / rng > 0.6:
                                                sweeps_detected.append({"type": "lower_sweep", "bar": ci, "rejection_from": round(l, 6)})

                                # 5. Candle Pattern Detection (last 3 candles)
                                candle_patterns = []
                                if len(candles) >= 3:
                                    for ci in range(-3, 0):
                                        c = candles[ci]
                                        o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
                                        body = abs(cl - o)
                                        rng = h - l
                                        if rng > 0:
                                            body_ratio = body / rng
                                            if body_ratio < 0.1:
                                                candle_patterns.append({"bar": ci, "pattern": "doji"})
                                            elif body_ratio > 0.7 and cl > o:
                                                candle_patterns.append({"bar": ci, "pattern": "strong_bullish"})
                                            elif body_ratio > 0.7 and cl < o:
                                                candle_patterns.append({"bar": ci, "pattern": "strong_bearish"})
                                    
                                    # Engulfing
                                    c_prev = candles[-2]
                                    c_curr = candles[-1]
                                    po, pc = float(c_prev["open"]), float(c_prev["close"])
                                    co, cc = float(c_curr["open"]), float(c_curr["close"])
                                    if pc < po and cc > co and cc > po and co < pc:
                                        candle_patterns.append({"bar": -1, "pattern": "bullish_engulfing"})
                                    elif pc > po and cc < co and cc < po and co > pc:
                                        candle_patterns.append({"bar": -1, "pattern": "bearish_engulfing"})

                                # 6. EMA-20 Distance (overextension check)
                                ema_distance_pct = round(((current_price - ema_20) / ema_20) * 100, 2) if ema_20 > 0 else 0

                                return {
                                    "symbol": symbol,
                                    "rsi_14": rsi,
                                    "ema_20": ema_20,
                                    "ema_trend": ema_trend,
                                    "ema_distance_pct": ema_distance_pct,
                                    "macd": round(macd_val, 6),
                                    "macd_val": round(macd_val, 6),
                                    "macd_signal": round(signal_val, 6),
                                    "macd_label": macd_signal_label,
                                    "macd_histogram": macd_histogram,
                                    "histogram_momentum": histogram_momentum,
                                    "atr_14": round(atr_14, 6),
                                    "atr_pct": atr_pct,
                                    # Algorithmic signals (deterministic, no LLM needed)
                                    "algo_signals": {
                                        "rsi_divergence": rsi_divergence,
                                        "macd_crossover": macd_crossover,
                                        "liquidity_sweeps": sweeps_detected,
                                        "candle_patterns": candle_patterns
                                    }
                                }
                            else:
                                self._log(f"⚠️ [MarketDataService] Недостаточно истории для индикаторов {symbol} (нужно >= 35, есть {len(closes)}).")
                                return {
                                    "symbol": symbol,
                                    "rsi_14": 50.0,
                                    "ema_20": 0.0,
                                    "ema_trend": "flat",
                                    "ema_distance_pct": 0.0,
                                    "macd_val": 0.0,
                                    "macd_signal": "neutral",
                                    "macd_histogram": 0.0,
                                    "histogram_momentum": "neutral",
                                    "atr_14": 0.0,
                                    "atr_pct": 0.0,
                                    "algo_signals": {
                                        "rsi_divergence": "none",
                                        "macd_crossover": "none",
                                        "liquidity_sweeps": [],
                                        "candle_patterns": []
                                    }
                                }
                        else:
                            self._log(f"⚠️ [MarketDataService] Пустой массив свечей от Kraken для {symbol}.")
                            return {
                                "symbol": symbol,
                                "rsi_14": 50.0,
                                "ema_20": 0.0,
                                "ema_trend": "flat",
                                "ema_distance_pct": 0.0,
                                "macd_val": 0.0,
                                "macd_signal": "neutral",
                                "macd_histogram": 0.0,
                                "histogram_momentum": "neutral",
                                "atr_14": 0.0,
                                "atr_pct": 0.0,
                                "algo_signals": {
                                    "rsi_divergence": "none",
                                    "macd_crossover": "none",
                                    "liquidity_sweeps": [],
                                    "candle_patterns": []
                                }
                            }

        except Exception as e:
            self._log(f"⚠️ [MarketDataService] Ошибка расчёта индикаторов: {e}")
            raise Exception(f"Не удалось получить индикаторы для {symbol}") from e

    async def fetch_news_sentiment(self, symbol: str) -> Dict[str, Any]:
        """Fetches real Crypto Fear & Greed Index from Alternative.me."""
        url = "https://api.alternative.me/fng/"
        try:
            headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"}
            session = await self._get_session()
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        item = data.get("data", [{}])[0]
                        val = float(item.get("value", 50))
                        classif = item.get("value_classification", "Neutral")
                        return {
                            "symbol": symbol,
                            "sentiment_score": round(val, 2),
                            "latest_event": f"Market Sentiment: {classif} (Fear & Greed Index: {int(val)})"
                        }
        except Exception as e:
            self._log(f"⚠️ [MarketDataService] Ошибка загрузки сентимента для {symbol}: {e}. Используем Neutral.")
            
        return {
            "symbol": symbol,
            "sentiment_score": 50.0,
            "latest_event": "Market Sentiment: Neutral (Fallback due to API error)"
        }

    async def fetch_oi_funding(self, symbol: str) -> Dict[str, Any]:
        """Fetches real Open Interest and funding rates from Nado DEX."""
        if not self.is_nado or not self.nado_client:
            return {"symbol": symbol, "open_interest": 0.0, "open_interest_trend": "neutral", "funding_rate": 0.0001}
            
        try:
            import asyncio
            from nado_protocol.indexer_client.types.query import IndexerMarketSnapshotsParams, IndexerMarketSnapshotInterval
            
            base_symbol = symbol.split('-')[0].upper()
            product_id = self.product_map.get(base_symbol)
            if product_id is None:
                return {"symbol": symbol, "open_interest": 0.0, "open_interest_trend": "neutral", "funding_rate": 0.0001}
                
            params = IndexerMarketSnapshotsParams(interval=IndexerMarketSnapshotInterval(count=1, granularity=86400))
            snapshots = await asyncio.to_thread(self.nado_client.market.get_market_snapshots, params)
            
            if snapshots and snapshots.snapshots:
                snap = snapshots.snapshots[0]
                vid = str(product_id)
                open_interest = float(snap.open_interests.get(vid, 0)) / 1e18
                funding_rate = float(snap.funding_rates.get(vid, 0)) / 1e18
                
                # QW #4: Track OI Trend
                oi_trend = "neutral"
                if symbol in self._oi_history:
                    prev_oi = self._oi_history[symbol]
                    if open_interest > prev_oi * 1.01: # 1% increase
                        oi_trend = "rising"
                    elif open_interest < prev_oi * 0.99: # 1% decrease
                        oi_trend = "falling"
                    else:
                        oi_trend = "stable"
                        
                self._oi_history[symbol] = open_interest
                self._save_oi_history()

                return {
                    "symbol": symbol,
                    "open_interest": open_interest,
                    "open_interest_trend": oi_trend,
                    "funding_rate": round(funding_rate, 8)
                }
        except Exception as e:
            self._log(f"⚠️ [MarketDataService] Failed to fetch Nado OI/Funding for {symbol}: {e}")
            
        return {"symbol": symbol, "open_interest": 0.0, "open_interest_trend": "neutral", "funding_rate": 0.0001}

    async def fetch_margin_requirements(self, symbol: str) -> Dict[str, Any]:
        """Fetches real maintenance margin parameters from Nado DEX SDK."""
        if not self.is_nado or not self.nado_client:
            return {"maintenance_margin_pct": 0.01}
            
        try:
            import asyncio
            base_symbol = symbol.split('-')[0].upper()
            product_id = self.product_map.get(base_symbol)
            
            if product_id is not None:
                markets = await asyncio.to_thread(self.nado_client.market.get_all_engine_markets)
                for p in markets.perp_products:
                    if p.product_id == product_id:
                        # Extract maintenance margin from risk parameters
                        try:
                            lwm = float(p.risk.long_weight_maintenance_x18) / 1e18
                            if lwm > 0:
                                mm = 1.0 - lwm
                                return {"maintenance_margin_pct": round(mm, 4)}
                        except Exception:
                            # Fallback if SDK structures differ slightly
                            pass
                        break
        except Exception as e:
            self._log(f"⚠️ [MarketDataService] Failed to fetch Nado margin for {symbol}: {e}")
            
        return {"maintenance_margin_pct": 0.01} # fallback to conservative 1%

    async def fetch_all_market_data(self, symbol: str) -> Dict[str, Any]:
        """
        Aggregates all real-time market data asynchronously including Multi-Timeframe Alignment.
        Includes a 3-attempt retry mechanism for resilience against temporary API failures.
        """
        for attempt in range(3):
            try:
                mtf, ohlcv, ob, oi, indicators, news, margin = await asyncio.gather(
                    self.fetch_multi_timeframe(symbol),
                    self.fetch_ohlcv(symbol),
                    self.fetch_order_book(symbol),
                    self.fetch_oi_funding(symbol),
                    self.fetch_indicators(symbol),
                    self.fetch_news_sentiment(symbol),
                    self.fetch_margin_requirements(symbol)
                )

                # Merge margin into derivatives_data
                if isinstance(oi, dict) and isinstance(margin, dict):
                    oi.update(margin)

                return {
                    "exchange": self.exchange_name,
                    "symbol": symbol,
                    "multi_timeframe": mtf,
                    "price_data": ohlcv,
                    "order_book_data": ob,
                    "derivatives_data": oi,
                    "indicators": indicators,
                    "news_data": news
                }
            except Exception as e:
                if attempt < 2:
                    self._log(f"⚠️ [MarketDataService] Ошибка агрегации для {symbol}: {e}. Повтор ({attempt+1}/3) через 2 сек...")
                    await asyncio.sleep(2)
                else:
                    self._log(f"❌ [MarketDataService] Критическая ошибка агрегации для {symbol} после 3 попыток.")
                    raise
