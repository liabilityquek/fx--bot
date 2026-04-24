"""OANDA broker implementation using v20 API."""

import logging
import time
from typing import List, Optional, Dict, Callable, Any
from datetime import datetime

from oandapyV20 import API
from oandapyV20.exceptions import V20Error
import oandapyV20.endpoints.accounts as accounts
import oandapyV20.endpoints.pricing as pricing
import oandapyV20.endpoints.trades as trades
import oandapyV20.endpoints.positions as positions
import oandapyV20.endpoints.orders as orders
import oandapyV20.endpoints.instruments as instruments

from config.settings import settings
from config.pairs import PAIR_INFO
from .base import (
    BaseBroker, Trade, Position, AccountInfo,
    OrderSide, OrderStatus
)


def _fmt_price(pair: str, price: float) -> str:
    """Format a price to OANDA's required decimal precision for the pair."""
    decimals = PAIR_INFO.get(pair, {}).get('pip_decimal', 4) + 1
    return f"{price:.{decimals}f}"


class OandaBroker(BaseBroker):
    """OANDA broker implementation."""
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        """
        Initialize OANDA broker.
        
        Args:
            logger: Logger instance
        """
        self.logger = logger or logging.getLogger('oanda_broker')
        self.api = None
        self.account_id = settings.OANDA_ACCOUNT_ID
        self.connected = False
    
    def _with_retry(self, fn: Callable[[], Any], retries: int = 3, base_delay: float = 2.0) -> Any:
        """
        Execute fn() with exponential-backoff retry on transient errors.

        V20Error (OANDA logic errors) are re-raised immediately — they indicate
        a problem with the request itself, not a network blip.

        Args:
            fn: Zero-argument callable wrapping one broker API call.
            retries: Maximum number of attempts (default 3).
            base_delay: Initial sleep seconds; doubles each attempt (2s, 4s, 8s).

        Returns:
            Whatever fn() returns.

        Raises:
            The last exception if all attempts fail.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            try:
                return fn()
            except V20Error:
                raise  # Broker logic errors — don't retry
            except Exception as exc:
                last_exc = exc
                if attempt < retries - 1:
                    delay = base_delay * (2 ** attempt)
                    self.logger.warning(
                        f"Broker call failed (attempt {attempt + 1}/{retries}): {exc} "
                        f"— retrying in {delay:.0f}s"
                    )
                    time.sleep(delay)
        raise last_exc

    def connect(self) -> bool:
        """Establish connection to OANDA API."""
        try:
            # Initialize API client — 15s timeout applied globally to all requests
            self.api = API(
                access_token=settings.OANDA_API_KEY,
                environment=settings.OANDA_ENVIRONMENT,
                request_params={"timeout": 15}
            )
            
            # Test connection by fetching account info
            account_info = self.get_account_info()
            
            if account_info:
                self.connected = True
                self.logger.info(
                    f"✅ Connected to OANDA ({settings.OANDA_ENVIRONMENT}) - "
                    f"Account verified"
                )
                return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"Failed to connect to OANDA: {e}")
            return False
    
    def get_account_info(self) -> Optional[AccountInfo]:
        """Get current account information."""
        try:
            endpoint = accounts.AccountDetails(accountID=self.account_id)
            response = self._with_retry(lambda: self.api.request(endpoint))
            
            account_data = response['account']
            
            return AccountInfo(
                account_id=account_data['id'],
                balance=float(account_data['balance']),
                nav=float(account_data['NAV']),
                margin_used=float(account_data.get('marginUsed', 0)),
                margin_available=float(account_data.get('marginAvailable', 0)),
                unrealized_pnl=float(account_data.get('unrealizedPL', 0)),
                open_trade_count=int(account_data.get('openTradeCount', 0)),
                currency=account_data.get('currency', 'USD')
            )
            
        except V20Error as e:
            self.logger.error(f"OANDA API error getting account info: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Error getting account info: {e}")
            return None
    
    def get_current_price(self, pair: str) -> Optional[Dict[str, float]]:
        """Get current bid/ask prices for a pair."""
        try:
            params = {"instruments": pair}
            endpoint = pricing.PricingInfo(
                accountID=self.account_id,
                params=params
            )
            response = self._with_retry(lambda: self.api.request(endpoint))
            
            if response['prices']:
                price_data = response['prices'][0]
                return {
                    'bid': float(price_data['bids'][0]['price']),
                    'ask': float(price_data['asks'][0]['price']),
                    'spread': float(price_data['asks'][0]['price']) - float(price_data['bids'][0]['price']),
                    'time': price_data['time']
                }
            
            return None
            
        except V20Error as e:
            self.logger.error(f"OANDA API error getting price for {pair}: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Error getting price for {pair}: {e}")
            return None
    
    def _get_current_prices(self, instrument_list: List[str]) -> Dict[str, float]:
        """Fetch mid (bid+ask)/2 prices for a list of OANDA instruments."""
        try:
            params = {"instruments": ",".join(instrument_list)}
            endpoint = pricing.PricingInfo(accountID=self.account_id, params=params)
            response = self._with_retry(lambda: self.api.request(endpoint))
            result = {}
            for price_data in response.get('prices', []):
                bid = float(price_data['bids'][0]['price'])
                ask = float(price_data['asks'][0]['price'])
                result[price_data['instrument']] = (bid + ask) / 2
            return result
        except Exception as e:
            self.logger.warning(f"Could not fetch current prices: {e}")
            return {}

    def get_open_trades(self) -> List[Trade]:
        """Get all open trades."""
        try:
            endpoint = trades.OpenTrades(accountID=self.account_id)
            response = self._with_retry(lambda: self.api.request(endpoint))

            trade_list_raw = response.get('trades', [])
            if not trade_list_raw:
                return []

            # Fetch live mid-prices for all open instruments in one API call
            instruments_set = {t['instrument'] for t in trade_list_raw}
            current_prices = self._get_current_prices(list(instruments_set))

            trade_list = []
            for trade_data in trade_list_raw:
                instrument = trade_data['instrument']
                trade = Trade(
                    trade_id=trade_data['id'],
                    pair=instrument,
                    side=OrderSide.BUY if float(trade_data['currentUnits']) > 0 else OrderSide.SELL,
                    units=abs(int(float(trade_data['currentUnits']))),
                    entry_price=float(trade_data['price']),
                    current_price=current_prices.get(instrument, float(trade_data['price'])),
                    stop_loss=float(trade_data.get('stopLossOrder', {}).get('price', 0)) or None,
                    take_profit=float(trade_data.get('takeProfitOrder', {}).get('price', 0)) or None,
                    unrealized_pnl=float(trade_data.get('unrealizedPL', 0)),
                    open_time=datetime.fromisoformat(trade_data['openTime'].replace('Z', '+00:00'))
                )
                trade_list.append(trade)

            return trade_list

        except V20Error as e:
            self.logger.error(f"OANDA API error getting open trades: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Error getting open trades: {e}")
            return []
    
    def get_positions(self) -> List[Position]:
        """Get all open positions."""
        try:
            endpoint = positions.OpenPositions(accountID=self.account_id)
            response = self._with_retry(lambda: self.api.request(endpoint))
            
            position_list = []
            
            for pos_data in response.get('positions', []):
                # OANDA separates long and short positions
                long_units = int(float(pos_data.get('long', {}).get('units', 0)))
                short_units = int(float(pos_data.get('short', {}).get('units', 0)))
                net_units = long_units + short_units  # short_units will be negative
                
                if net_units == 0:
                    continue  # Skip flat positions
                
                # Get trades for this position
                all_trades = self.get_open_trades()
                position_trades = [t for t in all_trades if t.pair == pos_data['instrument']]
                
                # Calculate average price
                if long_units != 0:
                    avg_price = float(pos_data['long'].get('averagePrice', 0))
                else:
                    avg_price = float(pos_data['short'].get('averagePrice', 0))
                
                position = Position(
                    pair=pos_data['instrument'],
                    net_units=net_units,
                    average_price=avg_price,
                    unrealized_pnl=float(pos_data.get('unrealizedPL', 0)),
                    trades=position_trades
                )
                position_list.append(position)
            
            return position_list
            
        except V20Error as e:
            self.logger.error(f"OANDA API error getting positions: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Error getting positions: {e}")
            return []
    
    def get_position(self, pair: str) -> Optional[Position]:
        """Get position for specific pair."""
        positions_list = self.get_positions()
        for position in positions_list:
            if position.pair == pair:
                return position
        return None
    
    def place_market_order(
        self,
        pair: str,
        side: OrderSide,
        units: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None
    ) -> Optional[str]:
        """Place a market order."""
        try:
            # OANDA uses positive units for buy, negative for sell
            oanda_units = units if side == OrderSide.BUY else -units
            
            # Build order specification
            order_spec = {
                "order": {
                    "type": "MARKET",
                    "instrument": pair,
                    "units": str(oanda_units),
                    "timeInForce": "FOK",  # Fill or Kill
                    "positionFill": "DEFAULT"
                }
            }
            
            # Add stop loss if provided
            if stop_loss:
                order_spec["order"]["stopLossOnFill"] = {
                    "price": _fmt_price(pair, stop_loss)
                }

            # Add take profit if provided
            if take_profit:
                order_spec["order"]["takeProfitOnFill"] = {
                    "price": _fmt_price(pair, take_profit)
                }
            
            # Place the order
            endpoint = orders.OrderCreate(
                accountID=self.account_id,
                data=order_spec
            )
            response = self.api.request(endpoint)
            
            # Extract trade ID from response
            if 'orderFillTransaction' in response:
                trade_opened = response['orderFillTransaction'].get('tradeOpened')
                if trade_opened:
                    trade_id = trade_opened['tradeID']
                    self.logger.info(
                        f"✅ Order placed: {pair} {side.value.upper()} {units} units | "
                        f"Trade ID: {trade_id}"
                    )
                    return trade_id
            
            self.logger.warning(f"Order placed but no trade ID returned: {response}")
            return None
            
        except V20Error as e:
            self.logger.error(f"OANDA API error placing order: {e}")
            return None
        except Exception as e:
            self.logger.error(f"Error placing order: {e}")
            return None
    
    def close_trade(self, trade_id: str) -> bool:
        """Close a specific trade."""
        try:
            endpoint = trades.TradeClose(
                accountID=self.account_id,
                tradeID=trade_id
            )
            response = self.api.request(endpoint)
            
            if 'orderFillTransaction' in response:
                pnl = float(response['orderFillTransaction'].get('pl', 0))
                self.logger.info(f"✅ Trade {trade_id} closed | P/L: ${pnl:.2f}")
                return True
            
            return False
            
        except V20Error as e:
            self.logger.error(f"OANDA API error closing trade {trade_id}: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Error closing trade {trade_id}: {e}")
            return False
    
    def close_position(self, pair: str) -> bool:
        """Close entire position for a pair."""
        try:
            # Close long units
            endpoint_long = positions.PositionClose(
                accountID=self.account_id,
                instrument=pair,
                data={"longUnits": "ALL"}
            )
            
            # Close short units
            endpoint_short = positions.PositionClose(
                accountID=self.account_id,
                instrument=pair,
                data={"shortUnits": "ALL"}
            )
            
            # Try closing both (one will fail if no position in that direction)
            try:
                self.api.request(endpoint_long)
            except V20Error as e:
                if 'NO_UNITS_TO_CLOSE' not in str(e) and 'closeoutPosition' not in str(e):
                    self.logger.warning(f"Unexpected error closing long position for {pair}: {e}")

            try:
                self.api.request(endpoint_short)
            except V20Error as e:
                if 'NO_UNITS_TO_CLOSE' not in str(e) and 'closeoutPosition' not in str(e):
                    self.logger.warning(f"Unexpected error closing short position for {pair}: {e}")
            
            self.logger.info(f"✅ Position closed: {pair}")
            return True
            
        except Exception as e:
            self.logger.error(f"Error closing position {pair}: {e}")
            return False
    
    def modify_trade(
        self,
        trade_id: str,
        pair: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None
    ) -> bool:
        """Modify stop loss and/or take profit for a trade."""
        try:
            # Get current trade details
            endpoint_details = trades.TradeDetails(
                accountID=self.account_id,
                tradeID=trade_id
            )
            trade_data = self.api.request(endpoint_details)

            # Modify stop loss
            if stop_loss is not None:
                sl_spec = {
                    "stopLoss": {
                        "price": _fmt_price(pair, stop_loss),
                        "timeInForce": "GTC"
                    }
                }
                endpoint_sl = trades.TradeCRCDO(
                    accountID=self.account_id,
                    tradeID=trade_id,
                    data=sl_spec
                )
                self.api.request(endpoint_sl)

            # Modify take profit
            if take_profit is not None:
                tp_spec = {
                    "takeProfit": {
                        "price": _fmt_price(pair, take_profit),
                        "timeInForce": "GTC"
                    }
                }
                endpoint_tp = trades.TradeCRCDO(
                    accountID=self.account_id,
                    tradeID=trade_id,
                    data=tp_spec
                )
                self.api.request(endpoint_tp)
            
            self.logger.info(f"✅ Trade {trade_id} modified")
            return True
            
        except V20Error as e:
            self.logger.error(f"OANDA API error modifying trade {trade_id}: {e}")
            return False
        except Exception as e:
            self.logger.error(f"Error modifying trade {trade_id}: {e}")
            return False

    def get_historical_candles(
        self,
        pair: str,
        granularity: str = 'H1',
        count: int = 100
    ) -> List[Dict]:
        """
        Fetch historical OHLC candle data for a pair.

        Args:
            pair: Trading pair (e.g. 'EUR_USD')
            granularity: Candle granularity (e.g. 'M1', 'H1', 'D')
            count: Number of candles to retrieve

        Returns:
            List of candle dicts with keys: time, open, high, low, close, volume
        """
        try:
            params = {
                'granularity': granularity,
                'count': count,
                'price': 'M'  # Mid prices
            }
            endpoint = instruments.InstrumentsCandles(instrument=pair, params=params)
            response = self.api.request(endpoint)

            candles = []
            for candle in response.get('candles', []):
                if not candle.get('complete', False):
                    continue
                mid = candle.get('mid', {})
                candles.append({
                    'time': candle['time'],
                    'open': float(mid.get('o', 0)),
                    'high': float(mid.get('h', 0)),
                    'low': float(mid.get('l', 0)),
                    'close': float(mid.get('c', 0)),
                    'volume': int(candle.get('volume', 0))
                })

            return candles

        except V20Error as e:
            self.logger.error(f"OANDA API error fetching candles for {pair}: {e}")
            return []
        except Exception as e:
            self.logger.error(f"Error fetching candles for {pair}: {e}")
            return []
