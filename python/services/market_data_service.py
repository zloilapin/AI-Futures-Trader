import asyncio
import aiohttp
from typing import Dict, Any, List

class MarketDataService:
    """
    Real-time market data service for Nado DEX (Ink L2 by Kraken).
    Retrieves live price tickers, multi-timeframe candlesticks (15m, 1H, 4H), order book depth,
    technical indicators (RSI, EMA, MACD), and sentiment (Crypto Fear & Greed Index).
    """
    def __init__(self, exchange_name: str = "Nado_DEX (Ink L2)"):
        self.exchange_name = exchange_name

    async def fetch_active_perps(self, limit: int = 15) -> List[Dict[str, Any]]:
        """
        Dynamically fetches all available perpetual contracts from the exchange,
        sorts them by 24h quote volume, and returns the top `limit` symbols with enriched data.
        """
        url = "https://futures.kraken.com/derivatives/api/v3/tickers"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        tickers = data.get("tickers", [])
                        
                        # Filter only perpetuals with valid pairs
                        perps = [t for t in tickers if t.get("tag") == "perpetual" and t.get("pair")]
                        
                        # Sort by 24h quote volume
                        perps.sort(key=lambda x: float(x.get("volumeQuote", 0) or 0), reverse=True)
                        
                        top_symbols = []
                        for p in perps[:limit]:
                            pair = p.get("pair", "")
                            if ":" in pair:
                                base_asset = pair.split(":")[0]
                                if base_asset == "XBT":
                                    base_asset = "BTC"
                                    
                                vol = round(float(p.get("volumeQuote", 0)), 2)
                                change = round(float(p.get("change24h", 0)), 2)
                                
                                top_symbols.append({
                                    "symbol": base_asset,
                                    "vol24h": vol,
                                    "change24h": change
                                })
                                
                        if top_symbols:
                            return top_symbols
        except Exception as e:
            print(f"⚠️ [MarketDataService] Ошибка загрузки списка перпов: {e}")
            
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

    async def _fetch_ohlc_interval(self, symbol: str, interval_min: int) -> Dict[str, Any]:
        pair = self._normalize_pair(symbol)
        
        # Map integer minutes to Kraken Futures timeframe strings
        tf_map = {15: "15m", 60: "1h", 240: "4h", 1440: "1d"}
        interval_str = tf_map.get(interval_min, "15m")
        
        url = f"https://futures.kraken.com/api/charts/v1/trade/{pair}/{interval_str}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candles = data.get("candles", [])
                        if candles:
                            closes = [float(c["close"]) for c in candles[-20:]]
                            current_price = closes[-1]
                            price_5_ago = closes[-5] if len(closes) >= 5 else closes[0]
                            pct_diff = ((current_price - price_5_ago) / price_5_ago) * 100
                            trend = "BULLISH" if pct_diff > 0.2 else ("BEARISH" if pct_diff < -0.2 else "NEUTRAL")
                            vol = sum(float(c["volume"]) for c in candles[-20:])
                            return {
                                "interval_min": interval_min,
                                "current_price": round(current_price, 6),
                                "trend": trend,
                                "change_pct": round(pct_diff, 2),
                                "volume": round(vol * current_price, 2),
                                "candles_20": candles[-20:]
                            }
        except Exception as e:
            print(f"⚠️ [MarketDataService] Ошибка загрузки OHLC {interval_min}m для {symbol}: {e}")
            raise Exception(f"Не удалось получить OHLC {interval_min}m для {symbol}") from e

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
            "current_price": res["current_price"],
            "trend": res["trend"].lower(),
            "recent_volume": res["volume"],
            "candles_20": res.get("candles_20", [])
        }

    async def fetch_order_book(self, symbol: str) -> Dict[str, Any]:
        """Fetches real order book depth, spread, and wall strengths from Kraken Futures."""
        pair = self._normalize_pair(symbol)
        url = f"https://futures.kraken.com/derivatives/api/v3/orderbook?symbol={pair}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        order_book = data.get("orderBook", {})
                        bids = order_book.get("bids", [])
                        asks = order_book.get("asks", [])
                        if bids and asks:
                            best_bid = float(bids[0][0])
                            best_ask = float(asks[0][0])
                            spread = round(best_ask - best_bid, 4)
                            spread_pct = round((spread / best_bid) * 100, 4)
                            bid_vol = sum(float(b[1]) for b in bids)
                            ask_vol = sum(float(a[1]) for a in asks)
                            return {
                                "symbol": symbol,
                                "spread": spread,
                                "spread_pct": spread_pct,
                                "bid_volume": round(bid_vol, 4),
                                "ask_volume": round(ask_vol, 4),
                                "imbalance_ratio": round(bid_vol / (ask_vol + 1e-6), 2)
                            }
        except Exception as e:
            print(f"⚠️ [MarketDataService] Ошибка загрузки стакана: {e}")
            raise Exception(f"Не удалось получить стакан для {symbol}") from e

    async def fetch_indicators(self, symbol: str) -> Dict[str, Any]:
        """Fetches candle data and computes RSI-14, EMA-20, and MACD."""
        try:
            # We can use the existing _fetch_ohlc_interval to get 15m futures candles
            res = await self._fetch_ohlc_interval(symbol, 15)
            candles = res.get("candles_20", [])
            # We need a longer history to warm up EMA and MACD properly.
            # 200 candles is a good industry standard for stable EMA/MACD values.
            pair = self._normalize_pair(symbol)
            url = f"https://futures.kraken.com/api/charts/v1/trade/{pair}/15m"
            
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        candles = data.get("candles", [])
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
                                macd_signal = "bullish" if macd_val > signal_val else "bearish"

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
                                    "macd_val": round(macd_val, 6),
                                    "macd_signal": macd_signal,
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
        except Exception as e:
            print(f"⚠️ [MarketDataService] Ошибка расчёта индикаторов: {e}")
            raise Exception(f"Не удалось получить индикаторы для {symbol}") from e

    async def fetch_news_sentiment(self, symbol: str) -> Dict[str, Any]:
        """Fetches real Crypto Fear & Greed Index from Alternative.me."""
        url = "https://api.alternative.me/fng/"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
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
            print(f"⚠️ [MarketDataService] Ошибка загрузки сентимента: {e}")
            raise Exception(f"Не удалось получить сентимент для {symbol}") from e

    async def fetch_oi_funding(self, symbol: str) -> Dict[str, Any]:
        """Fetches real Open Interest and funding rates from Kraken Futures."""
        url = "https://futures.kraken.com/derivatives/api/v3/tickers"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        tickers = data.get("tickers", [])
                        
                        target_base = "XBT" if symbol.upper() == "BTC" else symbol.upper()
                        
                        for t in tickers:
                            if t.get("tag") == "perpetual" and t.get("pair"):
                                base_asset = t.get("pair").split(":")[0]
                                if base_asset == target_base:
                                    funding_rate = float(t.get("fundingRate", 0.0001) or 0.0001)
                                    open_interest = float(t.get("openInterest", 0) or 0)
                                    
                                    # QW #4: Track OI Trend
                                    if not hasattr(self, "_oi_history"):
                                        self._oi_history = {}
                                        
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

                                    return {
                                        "symbol": symbol,
                                        "open_interest": open_interest,
                                        "open_interest_trend": oi_trend,
                                        "funding_rate": round(funding_rate, 6)
                                    }
        except Exception as e:
            print(f"⚠️ [MarketDataService] Ошибка загрузки OI/Funding: {e}")
            raise Exception(f"Не удалось получить OI/Funding для {symbol}") from e

    async def fetch_all_market_data(self, symbol: str) -> Dict[str, Any]:
        """
        Aggregates all real-time market data asynchronously including Multi-Timeframe Alignment.
        """
        mtf, ohlcv, ob, oi, indicators, news = await asyncio.gather(
            self.fetch_multi_timeframe(symbol),
            self.fetch_ohlcv(symbol),
            self.fetch_order_book(symbol),
            self.fetch_oi_funding(symbol),
            self.fetch_indicators(symbol),
            self.fetch_news_sentiment(symbol)
        )

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
