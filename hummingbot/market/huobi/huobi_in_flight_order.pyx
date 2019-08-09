from decimal import Decimal
from typing import (
    Any,
    Dict,
    Optional
)

from hummingbot.core.event.events import (
    OrderType,
    TradeType
)
from hummingbot.market.huobi.huobi_market import HuobiMarket
from hummingbot.market.in_flight_order_base import InFlightOrderBase

s_decimal_0 = Decimal(0)


cdef class HuobiInFlightOrder(InFlightOrderBase):
    def __init__(self,
                 client_order_id: str,
                 exchange_order_id: str,
                 symbol: str,
                 order_type: OrderType,
                 trade_type: TradeType,
                 price: Decimal,
                 amount: Decimal,
                 initial_state: str = "submitted"):
        super().__init__(
            HuobiMarket,
            client_order_id,
            exchange_order_id,
            symbol,
            order_type,
            trade_type,
            price,
            amount,
            initial_state
        )
        self.trade_id_set = set()

    @property
    def is_done(self) -> bool:
        return self.last_state in {"filled", "canceled", "partial-filled", "cancelling", "partial-canceled"}

    @property
    def is_failure(self) -> bool:
        return self.last_state in {"canceled", "cancelling"}

    @property
    def is_cancelled(self) -> bool:
        return self.last_state in {"canceled", "cancelling", "partial-cancelled"}

    @classmethod
    def from_json(cls, data: Dict[str, Any]) -> InFlightOrderBase:
        cdef:
            HuobiInFlightOrder retval = HuobiInFlightOrder(
                client_order_id=data["client_order_id"],
                exchange_order_id=data["exchange_order_id"],
                symbol=data["symbol"],
                order_type=getattr(OrderType, data["order_type"]),
                trade_type=getattr(TradeType, data["trade_type"]),
                price=Decimal(data["price"]),
                amount=Decimal(data["amount"]),
                initial_state=data["last_state"]
            )
        retval.executed_amount_base = Decimal(data["executed_amount_base"])
        retval.executed_amount_quote = Decimal(data["executed_amount_quote"])
        retval.fee_asset = data["fee_asset"]
        retval.fee_paid = Decimal(data["fee_paid"])
        retval.last_state = data["last_state"]
        return retval

    def update_with_trade_id(self, sequence_id: int, order_id: str):
        if order_id != self.exchange_order_id or sequence_id in self.trade_id_set:
            # trade already recorded
            return
        self.trade_id_set.add(sequence_id)
        return sequence_id