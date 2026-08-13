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
        """Helper to convert symbols like BTC-USDC to Kraken's XBTUSD format."""
        s = symbol.upper().replace("-", "").replace("/", "").replace("USDC", "USD").replace("USDT", "USD")
        if s.startswith("BTC"):
            return "XBTUSD"
        
        # Ensure it ends with USD for Kraken Spot API
        if not s.endswith("USD"):
            s = f"{s}USD"
            
        return s

    async def _fetch_ohlc_interval(self, symbol: str, interval_min: int) -> Dict[str, Any]:
        pair = self._normalize_pair(symbol)
        url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval={interval_min}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = data.get("result", {})
                        key = list(result.keys())[0] if result else None
                        if key and key != "last":
                            candles = result[key]
                            closes = [float(c[4]) for c in candles[-20:]]
                            current_price = closes[-1]
                            price_5_ago = closes[-5] if len(closes) >= 5 else closes[0]
                            pct_diff = ((current_price - price_5_ago) / price_5_ago) * 100
                            trend = "BULLISH" if pct_diff > 0.2 else ("BEARISH" if pct_diff < -0.2 else "NEUTRAL")
                            vol = sum(float(c[6]) for c in candles[-20:])
                            return {
                                "interval_min": interval_min,
                                "current_price": round(current_price, 2),
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
            "volume_24h": res["volume"],
            "candles_20": res.get("candles_20", [])
        }

    async def fetch_order_book(self, symbol: str) -> Dict[str, Any]:
        """Fetches real order book depth, spread, and wall strengths from Kraken."""
        pair = self._normalize_pair(symbol)
        url = f"https://api.kraken.com/0/public/Depth?pair={pair}&count=20"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = data.get("result", {})
                        key = list(result.keys())[0] if result else None
                        if key:
                            bids = result[key].get("bids", [])
                            asks = result[key].get("asks", [])
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
        pair = self._normalize_pair(symbol)
        url = f"https://api.kraken.com/0/public/OHLC?pair={pair}&interval=15"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        result = data.get("result", {})
                        key = list(result.keys())[0] if result else None
                        if key and key != "last":
                            candles = result[key]
                            closes = [float(c[4]) for c in candles[-50:]]
                            highs = [float(c[2]) for c in candles[-50:]]
                            lows = [float(c[3]) for c in candles[-50:]]
                            if len(closes) >= 15:
                                gains = [max(0, closes[i] - closes[i-1]) for i in range(1, len(closes))]
                                losses = [max(0, closes[i-1] - closes[i]) for i in range(1, len(closes))]
                                avg_gain = sum(gains[-14:]) / 14
                                avg_loss = sum(losses[-14:]) / 14
                                rs = avg_gain / (avg_loss + 1e-6)
                                rsi = round(100 - (100 / (1 + rs)), 2)

                                def calc_ema(data, period):
                                    if len(data) < period: return sum(data)/len(data) if data else 0
                                    ema = sum(data[:period]) / period
                                    k = 2 / (period + 1)
                                    for price in data[period:]: ema = (price - ema) * k + ema
                                    return ema

                                ema_20_raw = calc_ema(closes, 20)
                                current_price = closes[-1]
                                ema_trend = "up" if current_price > ema_20_raw else "down"

                                ema_12_raw = calc_ema(closes, 12)
                                macd_val = ema_12_raw - ema_20_raw
                                ema_20 = round(ema_20_raw, 2)
                                macd_signal = "bullish" if macd_val > 0 else "bearish"

                                # ATR-14 (Average True Range)
                                tr_list = [max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1])) for i in range(1, len(closes))]
                                atr_14 = sum(tr_list[-14:]) / 14
                                atr_pct = round((atr_14 / current_price) * 100, 2)

                                return {
                                    "symbol": symbol,
                                    "rsi_14": rsi,
                                    "ema_20": ema_20,
                                    "ema_trend": ema_trend,
                                    "macd_val": round(macd_val, 2),
                                    "macd_signal": macd_signal,
                                    "atr_14": round(atr_14, 2),
                                    "atr_pct": atr_pct
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
                                    return {
                                        "symbol": symbol,
                                        "open_interest": open_interest,
                                        "open_interest_trend": "neutral",
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
