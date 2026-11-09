import pytest
from nado_protocol.utils.order import order_builder_id, order_builder_fee_rate, order_reduce_only
from nado_protocol.utils.expiration import OrderType
from services.nado_trading_service import NadoTradingService
from core.config import config


def test_builder_id_omitted_when_zero():
    """Ensure builder_id and fee rate are omitted when NADO_BUILDER_ID is 0 (default)."""
    orig_id = config.NADO_BUILDER_ID
    orig_fee = config.NADO_BUILDER_FEE_RATE
    try:
        config.NADO_BUILDER_ID = 0
        config.NADO_BUILDER_FEE_RATE = 0

        # Maker POST_ONLY
        appendix_post = NadoTradingService.build_order_appendix(order_type=OrderType.POST_ONLY)
        assert order_builder_id(appendix_post) is None
        assert order_builder_fee_rate(appendix_post) is None
        assert not order_reduce_only(appendix_post)

        # IOC Taker
        appendix_ioc = NadoTradingService.build_order_appendix(order_type=OrderType.IOC)
        assert order_builder_id(appendix_ioc) is None
        assert order_builder_fee_rate(appendix_ioc) is None
        assert not order_reduce_only(appendix_ioc)
    finally:
        config.NADO_BUILDER_ID = orig_id
        config.NADO_BUILDER_FEE_RATE = orig_fee


def test_builder_id_encoded_when_positive():
    """Ensure builder_id and fee rate are correctly packed into appendix when NADO_BUILDER_ID > 0."""
    orig_id = config.NADO_BUILDER_ID
    orig_fee = config.NADO_BUILDER_FEE_RATE
    try:
        config.NADO_BUILDER_ID = 77
        config.NADO_BUILDER_FEE_RATE = 25  # 2.5 bps

        appendix = NadoTradingService.build_order_appendix(order_type=OrderType.POST_ONLY)
        assert order_builder_id(appendix) == 77
        assert order_builder_fee_rate(appendix) == 25
        assert not order_reduce_only(appendix)

        # Reduce-only partial close
        appendix_ro = NadoTradingService.build_order_appendix(order_type=OrderType.IOC, reduce_only=True)
        assert order_builder_id(appendix_ro) == 77
        assert order_builder_fee_rate(appendix_ro) == 25
        assert order_reduce_only(appendix_ro) is True
    finally:
        config.NADO_BUILDER_ID = orig_id
        config.NADO_BUILDER_FEE_RATE = orig_fee


def test_builder_id_explicit_overrides():
    """Ensure explicit builder_id arguments override config values."""
    appendix_custom = NadoTradingService.build_order_appendix(
        order_type=OrderType.DEFAULT,
        builder_id=1234,
        builder_fee_rate=50
    )
    assert order_builder_id(appendix_custom) == 1234
    assert order_builder_fee_rate(appendix_custom) == 50

    appendix_zero = NadoTradingService.build_order_appendix(
        order_type=OrderType.DEFAULT,
        builder_id=0,
        builder_fee_rate=0
    )
    assert order_builder_id(appendix_zero) is None
    assert order_builder_fee_rate(appendix_zero) is None

