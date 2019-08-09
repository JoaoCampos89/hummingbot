#!/usr/bin/env python

import asyncio
import aiohttp
import logging
import pandas as pd
from typing import (
    AsyncIterable,
    Dict,
    List,
    Optional,
)
import zlib
import time
import ujson
import websockets
from websockets.exceptions import ConnectionClosed

from hummingbot.market.huobi.huobi_order_book import HuobiOrderBook
from hummingbot.core.utils import async_ttl_cache
from hummingbot.logger import HummingbotLogger
from hummingbot.core.data_type.order_book_tracker_data_source import OrderBookTrackerDataSource
from hummingbot.core.data_type.order_book_tracker_entry import OrderBookTrackerEntry
from hummingbot.core.data_type.order_book_message import OrderBookMessage

HUOBI_SYMBOLS_URL = "https://api.huobi.pro/v1/common/symbols"
HUOBI_TICKER_URL = "https://api.huobi.pro/market/tickers"
HUOBI_DEPTH_URL = "https://api.huobi.pro/market/depth"
HUOBI_MKT_WEBSOCKET = "wss://api.huobi.pro/ws"


class HuobiAPIOrderBookDataSource(OrderBookTrackerDataSource):

    MESSAGE_TIMEOUT = 30.0
    PING_TIMEOUT = 10.0

    _haobds_logger: Optional[HummingbotLogger] = None

    @classmethod
    def logger(cls) -> HummingbotLogger:
        if cls._haobds_logger is None:
            cls._haobds_logger = logging.getLogger(__name__)
        return cls._haobds_logger

    def __init__(self, symbols: Optional[List[str]] = None):
        super().__init__()
        self._symbols: Optional[List[str]] = symbols

    @classmethod
    @async_ttl_cache(ttl=60 * 30, maxsize=1)
    async def get_active_exchange_markets(cls) -> pd.DataFrame:
        """
        Returned data frame should have symbol as index and include usd volume, baseAsset and quoteAsset
        """
        async with aiohttp.ClientSession() as client:

            market_response, exchange_response = await asyncio.gather(
                client.get(HUOBI_TICKER_URL),
                client.get(HUOBI_SYMBOLS_URL)
            )
            market_response: aiohttp.ClientResponse = market_response
            exchange_response: aiohttp.ClientResponse = exchange_response

            if market_response.status != 200:
                raise IOError(f"Error fetching Huobi markets information. "
                              f"HTTP status is {market_response.status}.")
            if exchange_response.status != 200:
                raise IOError(f"Error fetching Huobi exchange information. "
                              f"HTTP status is {exchange_response.status}.")

            market_data = await market_response.json()
            exchange_data = await exchange_response.json()
            print(market_data)

            trading_pairs: Dict[str, any] = {item["symbol"]: {k: item[k] for k in ["base-currency", "quote-currency"]}
                                             for item in exchange_data["data"]
                                             if item["state"] == "online"}

            market_data: List[Dict[str, any]] = [{**item, **trading_pairs[item["symbol"]]}
                                                 for item in market_data["data"]
                                                 if item["symbol"] in trading_pairs]

            # Build the data frame.
            all_markets: pd.DataFrame = pd.DataFrame.from_records(data=market_data, index="symbol")
            all_markets.loc[:, "USDVolume"] = all_markets.amount
            all_markets.loc[:, "volume"] = all_markets.vol

            return all_markets.sort_values("USDVolume", ascending=False)
        print("Recieved active exchange markets")

    @property
    def order_book_class(self) -> HuobiOrderBook:
        return HuobiOrderBook

    async def get_trading_pairs(self) -> List[str]:
        if not self._symbols:
            try:
                active_markets: pd.DataFrame = await self.get_active_exchange_markets()
                self._symbols = active_markets.index.tolist()
            except Exception:
                self._symbols = []
                self.logger().network(
                    f"Error getting active exchange information.",
                    exc_info=True,
                    app_warning_msg=f"Error getting active exchange information. Check network connection."
                )
        print("Got tracker pairs")
        return self._symbols

    @staticmethod
    async def get_snapshot(client: aiohttp.ClientSession, trading_pair: str) -> Dict[str, any]:
            params: Dict = {"symbol": trading_pair, "type": "step0"}
            async with client.get(HUOBI_DEPTH_URL, params=params) as response:
                response: aiohttp.ClientResponse = response
                if response.status != 200:
                    raise IOError(f"Error fetching Huobi market snapshot for {trading_pair}. "
                                  f"HTTP status is {response.status}.")
                api_data = await response.read()
                data = ujson.loads(api_data)

                # Need to add the symbol into the snapshot message for the Kafka message queue.
                # Because otherwise, there'd be no way for the receiver to know which market the
                # snapshot belongs to.

                return data

    async def get_tracking_pairs(self) -> Dict[str, OrderBookTrackerEntry]:
        # Get the currently active markets
        async with aiohttp.ClientSession() as client:
            trading_pairs: List[str] = await self.get_trading_pairs()
            retval: Dict[str, OrderBookTrackerEntry] = {}

            number_of_pairs: int = len(trading_pairs)
            for index, trading_pair in enumerate(trading_pairs):
                try:
                    snapshot: Dict[str, any] = await self.get_snapshot(client, trading_pair)
                    snapshot_timestamp: float = time.time()
                    snapshot_msg: OrderBookMessage = self.order_book_class.snapshot_message_from_exchange(
                        snapshot,
                        snapshot_timestamp,
                        metadata={"symbol": trading_pair}
                    )
                    order_book: HuobiOrderBook = self.order_book_class.from_snapshot(snapshot_msg)
                    retval[trading_pair] = OrderBookTrackerEntry(trading_pair, snapshot_timestamp, order_book)
                    self.logger().info(f"Initialized order book for {trading_pair}. "
                                        f"{index+1}/{number_of_pairs} completed.")
                    # Huobi rate limit is 100 https requests per 10 seconds
                    await asyncio.sleep(0.4)
                except Exception:
                    self.logger().error(f"Error getting snapshot for {trading_pair}. ", exc_info=True)
                    await asyncio.sleep(5)
            return retval

    async def _inner_messages(self,
                              ws: websockets.WebSocketClientProtocol) -> AsyncIterable[str]:
        # Terminate the recv() loop as soon as the next message timed out, so the outer loop can reconnect.
        try:
            while True:
                try:
                    msg: str = await asyncio.wait_for(ws.recv(), timeout=self.MESSAGE_TIMEOUT)
                    yield msg
                except asyncio.TimeoutError:
                    try:
                        pong_waiter = await ws.ping()
                        await asyncio.wait_for(pong_waiter, timeout=self.PING_TIMEOUT)
                    except asyncio.TimeoutError:
                        raise
        except asyncio.TimeoutError:
            self.logger().warning("WebSocket ping timed out. Going to reconnect...")
            return
        except ConnectionClosed:
            return
        finally:
            await ws.close()

    async def listen_for_order_book_diffs(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        while True:
            try:
                trading_pairs: List[str] = await self.get_trading_pairs()
                async with websockets.connect(HUOBI_MKT_WEBSOCKET) as ws:
                    ws: websockets.WebSocketClientProtocol = ws
                    for trading_pair in trading_pairs:
                        subscribe_request: Dict[str, any] = {
                            "sub": f"market.{trading_pair}.depth.step0",
                            "id": "hummingbot"
                        }
                        await ws.send(ujson.dumps(subscribe_request))

                    async for raw_msg in self._inner_messages(ws):
                        ws_result = str(zlib.decompressobj(31).decompress(raw_msg), encoding="utf-8")
                        msg = ujson.loads(ws_result)
                        if "ping" in msg:
                            pong_data = {"pong", msg["ping"]}
                            await ws.send(ujson.dumps(pong_data))
                        elif "subbed" not in msg:
                            order_book_message: OrderBookMessage = self.order_book_class.diff_message_from_exchange(
                                msg, time.time())
                            output.put_nowait(order_book_message)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error with WebSocket connection. Retrying after 30 seconds...",
                                    exc_info=True)
                await asyncio.sleep(30.0)

    async def listen_for_order_book_snapshots(self, ev_loop: asyncio.BaseEventLoop, output: asyncio.Queue):
        while True:
            try:
                trading_pairs: List[str] = await self.get_trading_pairs()
                async with aiohttp.ClientSession() as client:
                    for trading_pair in trading_pairs:
                        try:
                            snapshot: Dict[str, any] = await self.get_snapshot(client, trading_pair)
                            snapshot_timestamp: float = time.time()
                            snapshot_msg: OrderBookMessage = self.order_book_class.snapshot_message_from_exchange(
                                snapshot,
                                snapshot_timestamp,
                                metadata={"symbol": trading_pair}
                            )
                            output.put_nowait(snapshot_msg)
                            self.logger().debug(f"Saved order book snapshot for {trading_pair}")
                            # Be careful not to go above Huobi's API rate limits.
                            await asyncio.sleep(5.0)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            self.logger().error("Unexpected error.", exc_info=True)
                            await asyncio.sleep(5.0)
                    this_hour: pd.Timestamp = pd.Timestamp.utcnow().replace(minute=0, second=0, microsecond=0)
                    next_hour: pd.Timestamp = this_hour + pd.Timedelta(hours=1)
                    delta: float = next_hour.timestamp() - time.time()
                    await asyncio.sleep(delta)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.logger().error("Unexpected error.", exc_info=True)
                await asyncio.sleep(5.0)