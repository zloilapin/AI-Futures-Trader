import asyncio
import time
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from agents.sentinel_agent import SentinelAgent
from services.nado_trading_service import NadoTradingService
from core.logger import TradeLogger
from core.pipeline import TradingPipeline

@pytest.fixture
def mock_logger():
    logger = MagicMock(spec=TradeLogger)
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    logger.debug = MagicMock()
    return logger

def test_sentinel_dynamic_cost_buffer_altcoin_vs_major(mock_logger):
    """Verifies that Sentinel scales cost buffer for altcoins (0.35%) vs majors (0.20%)."""
    agent = SentinelAgent(mock_logger)
    profile_rules = {
        "sentinel_be_atr": 1.0,
        "sentinel_trail_activation_atr": 2.0,
        "sentinel_trail_distance_atr": 1.5,
        "sentinel_min_improve_atr": 0.25
    }
    eval_atr = 100.0

    # 1. Major asset (BTC-USD) -> 0.20% (0.001 fee + 0.001 slippage) = $20 buffer on $10,000 entry
    pos_btc = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 10000.0,
        "sl_price": 9800.0,
        "highest_price": 10120.0,
        "protection_state": "PROTECTED"
    }
    market_btc = {"price_data": {"current_price": 10100.0}}
    res_btc = asyncio.run(agent.analyze(pos_btc, market_btc, profile_rules, eval_atr))
    assert res_btc["state"] == "BREAK_EVEN"
    assert res_btc["new_sl"] == pytest.approx(10020.0)

    # 2. Altcoin (SOL-USD) -> 0.35% (0.001 fee + 0.0025 slippage) = $35 buffer on $10,000 entry
    pos_sol = {
        "symbol": "SOL-USD",
        "direction": "LONG",
        "entry_price": 10000.0,
        "sl_price": 9800.0,
        "highest_price": 10120.0,
        "protection_state": "PROTECTED"
    }
    market_sol = {"price_data": {"current_price": 10100.0}}
    res_sol = asyncio.run(agent.analyze(pos_sol, market_sol, profile_rules, eval_atr))
    assert res_sol["state"] == "BREAK_EVEN"
    assert res_sol["new_sl"] == pytest.approx(10035.0)

    # 3. Custom override in profile_rules takes precedence
    custom_rules = dict(profile_rules)
    custom_rules["sentinel_fee_pct"] = 0.0005
    custom_rules["sentinel_slippage_pct"] = 0.0015
    res_custom = asyncio.run(agent.analyze(pos_sol, market_sol, custom_rules, eval_atr))
    # 0.0005 + 0.0015 = 0.0020 -> $20 buffer
    assert res_custom["new_sl"] == pytest.approx(10020.0)

def test_sentinel_min_improve_floor_suppresses_low_atr_churn(mock_logger):
    """Verifies that Sentinel enforces an absolute percentage floor on minimum improvement to suppress noise."""
    agent = SentinelAgent(mock_logger)
    profile_rules = {
        "sentinel_be_atr": 1.0,
        "sentinel_trail_activation_atr": 1.5,
        "sentinel_trail_distance_atr": 1.5,
        "sentinel_min_improve_atr": 0.25,
        "sentinel_min_improve_pct": 0.0005  # 0.05% of entry ($5.00 on $10,000)
    }
    low_atr = 2.0  # min_improve * eval_atr = 0.25 * 2 = $0.50 (very small)
    # But entry * 0.0005 = $5.00, so effective_min_improve = $5.00

    # Current SL is already trailed to 10040.0
    pos = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 10000.0,
        "sl_price": 10040.0,
        "highest_price": 10044.0,  # trail_target = 10044 - (1.5 * 2) = 10041.0 (worse than current SL)
        "protection_state": "TRAILING"
    }

    # Case A: Highest price moves up slightly to 10045.0
    # trail_target = 10045 - 3 = 10042.0 -> candidate_sl = max(10040, 10042) = 10042.0
    # Delta is $2.00, which is < $5.00 floor -> Must be suppressed!
    pos["highest_price"] = 10045.0
    market_data = {"price_data": {"current_price": 10044.0}}
    res_suppressed = asyncio.run(agent.analyze(pos, market_data, profile_rules, low_atr))
    assert res_suppressed["new_sl"] is None
    assert res_suppressed["reasoning"] == "No update required."

    # Case B: Highest price moves up enough to 10049.0
    # trail_target = 10049 - 3 = 10046.0 -> Delta is $6.00 >= $5.00 floor -> Must trigger update!
    pos["highest_price"] = 10049.0
    market_data["price_data"]["current_price"] = 10048.0
    res_passed = asyncio.run(agent.analyze(pos, market_data, profile_rules, low_atr))
    assert res_passed["new_sl"] == pytest.approx(10046.0)
    assert res_passed["state"] == "TRAILING"

def test_sentinel_defensive_sanitization_and_extreme_coherence(mock_logger):
    """Verifies Sentinel defensive input sanitization and coherence of highest/lowest prices."""
    agent = SentinelAgent(mock_logger)
    profile_rules = {}

    # Invalid dictionaries
    assert asyncio.run(agent.analyze(None, {}, profile_rules, 10.0))["new_sl"] is None
    assert asyncio.run(agent.analyze({}, None, profile_rules, 10.0))["new_sl"] is None

    # Zero or missing price
    bad_pos = {"entry_price": 0, "sl_price": 0, "direction": "LONG"}
    bad_market = {"price_data": {"current_price": 0}}
    assert asyncio.run(agent.analyze(bad_pos, bad_market, profile_rules, 10.0))["new_sl"] is None

    # Missing highest/lowest gracefully initialises to entry / current_price
    pos_long = {
        "symbol": "BTC-USD",
        "direction": "LONG",
        "entry_price": 50000.0,
        "sl_price": 49000.0,
        "highest_price": 0.0,  # uninitialised
        "protection_state": "PROTECTED"
    }
    market = {"price_data": {"current_price": 52000.0}}
    res = asyncio.run(agent.analyze(pos_long, market, {"sentinel_be_atr": 1.0, "sentinel_trail_activation_atr": 1.5, "sentinel_trail_distance_atr": 1.0}, 500.0))
    # Profit = 2000 >= 1.5 * 500 (750), trailing triggers: highest is coerced to max(0, 50000, 52000) = 52000
    # target = 52000 - 500 = 51500
    assert res["state"] == "TRAILING"
    assert res["new_sl"] == pytest.approx(51500.0)

def test_nado_update_stop_loss_atomic_protection_state_persistence(tmp_path):
    """Verifies that update_stop_loss updates and persists both sl_price and protection_state atomically."""
    with patch("core.state_store.StateStore.load", return_value={}):
        service = NadoTradingService()
        service.is_connected = True
        service.active_positions["ETH-USD"] = {
            "symbol": "ETH-USD",
            "direction": "LONG",
            "entry_price": 3000.0,
            "sl_price": 2900.0,
            "protection_state": "PROTECTED",
            "sl_digest": None  # Software stop-loss
        }

        with patch.object(service, "_save_positions") as mock_save:
            success = asyncio.run(service.update_stop_loss("ETH-USD", 3010.0, new_protection_state="BREAK_EVEN"))
            assert success is True
            assert service.active_positions["ETH-USD"]["sl_price"] == 3010.0
            assert service.active_positions["ETH-USD"]["protection_state"] == "BREAK_EVEN"
            mock_save.assert_called_once()

def test_nado_update_stop_loss_monotonic_guard():
    """Verifies that update_stop_loss rejects adverse SL modifications."""
    with patch("core.state_store.StateStore.load", return_value={}):
        service = NadoTradingService()
        service.is_connected = True

        # LONG: cannot worsen SL (cannot lower it)
        service.active_positions["BTC-USD"] = {
            "direction": "LONG",
            "entry_price": 60000.0,
            "sl_price": 60050.0,
            "sl_digest": None
        }
        res_long_worse = asyncio.run(service.update_stop_loss("BTC-USD", 59900.0))
        assert res_long_worse is False
        assert service.active_positions["BTC-USD"]["sl_price"] == 60050.0

        # SHORT: cannot worsen SL (cannot raise it)
        service.active_positions["SOL-USD"] = {
            "direction": "SHORT",
            "entry_price": 150.0,
            "sl_price": 149.0,
            "sl_digest": None
        }
        res_short_worse = asyncio.run(service.update_stop_loss("SOL-USD", 152.0))
        assert res_short_worse is False
        assert service.active_positions["SOL-USD"]["sl_price"] == 149.0

def test_nado_update_stop_loss_on_chain_retry_and_software_fallback():
    """Verifies that when on-chain placement fails after retries, service falls back to software SL without panic close."""
    with patch("core.state_store.StateStore.load", return_value={}):
        service = NadoTradingService()
        service.is_connected = True
        service.product_map = {"BTC": 1, "BTC-USD": 1}
        service.client = MagicMock()
        service.default_subaccount_id = "0x1234"

        service.active_positions["BTC-USD"] = {
            "direction": "LONG",
            "entry_price": 60000.0,
            "sl_price": 59000.0,
            "product_id": 1,
            "sl_digest": "0xold_digest",
            "sender": "0x1234",
            "trigger_amount_x18": str(10**18),
            "sl_type": "oracle_price_below",
            "unverified_triggers": False
        }

        with patch.object(service, "_get_market_parameters", return_value={"price_increment_x18": 10**18}):
            with patch.object(service, "_cancel_trigger_digests", new_callable=AsyncMock) as mock_cancel:
                with patch.object(service, "force_close_position", new_callable=AsyncMock) as mock_force_close:
                    # Simulate all 3 on-chain placement attempts failing due to network error
                    service.client.market.place_price_trigger_order.side_effect = ConnectionError("Node RPC timeout")

                    success = asyncio.run(service.update_stop_loss("BTC-USD", 60100.0, new_protection_state="BREAK_EVEN"))

                    # Must succeed via software stop-loss fallback
                    assert success is True
                    # Old digest was cancelled
                    mock_cancel.assert_called_once_with(1, ["0xold_digest"], sender="0x1234")
                    # sl_price is updated to new level for Fast Price Monitor
                    assert service.active_positions["BTC-USD"]["sl_price"] == 60100.0
                    assert service.active_positions["BTC-USD"]["sl_digest"] is None
                    assert service.active_positions["BTC-USD"]["unverified_triggers"] is True
                    # MUST NOT execute emergency panic market closure
                    mock_force_close.assert_not_called()

def test_nado_fast_price_monitor_aborts_sl_update_if_closing():
    """Verifies that update_stop_loss aborts immediately if Fast Price Monitor has marked position as closing."""
    with patch("core.state_store.StateStore.load", return_value={}):
        service = NadoTradingService()
        service.is_connected = True
        service.active_positions["BTC-USD"] = {
            "direction": "LONG",
            "entry_price": 60000.0,
            "sl_price": 59000.0,
            "is_closing": True
        }
        res = asyncio.run(service.update_stop_loss("BTC-USD", 60100.0))
        assert res is False
        assert service.active_positions["BTC-USD"]["sl_price"] == 59000.0

def test_nado_sync_with_exchange_extremes_initialization():
    """Verifies that sync_with_exchange coherently bounds highest_price and lowest_price."""
    with patch("core.state_store.StateStore.load", return_value={}):
        service = NadoTradingService()
        service.is_connected = True
        service.default_subaccount_id = "0x1234"
        service.product_map = {"BTC": 1, "BTC-USD": 1, "SOL": 2, "SOL-USD": 2}
        service.client = MagicMock()
        service.wallet = MagicMock()
        service.wallet.get_address.return_value = "0xOwner"

        mock_positions = [
            {"symbol": "BTC-USD", "direction": "LONG", "entry_price": 60000.0, "size_usd": 100.0, "_product_id": 1},
            {"symbol": "SOL-USD", "direction": "SHORT", "entry_price": 150.0, "size_usd": 50.0, "_product_id": 2}
        ]

        # Saved state has inverted/corrupt extremes
        mock_saved = {
            "BTC-USD": {"highest_price": 55000.0, "lowest_price": 54000.0, "protection_state": "PROTECTED", "sl_price": 58000.0},
            "SOL-USD": {"highest_price": 160.0, "lowest_price": 180.0, "protection_state": "PROTECTED", "sl_price": 160.0}
        }

        with patch.object(service, "get_active_positions", new_callable=AsyncMock, return_value=mock_positions):
            with patch.object(service, "_load_positions", return_value=mock_saved):
                with patch.object(service, "_fetch_trigger_orders", new_callable=AsyncMock, return_value=[]):
                    asyncio.run(service.sync_with_exchange())

                    # For LONG BTC: highest_price must be at least entry (60,000)
                    btc_tracked = service.active_positions["BTC-USD"]
                    assert btc_tracked["highest_price"] >= 60000.0

                    # For SHORT SOL: lowest_price must be at most entry (150)
                    sol_tracked = service.active_positions["SOL-USD"]
                    assert sol_tracked["lowest_price"] <= 150.0
