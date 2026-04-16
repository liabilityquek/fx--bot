"""yfinance client for historical FX data.

Replaces Alpha Vantage for historical OHLC data.
yfinance uses Yahoo Finance data which has no API key requirement
and better rate limits for forex data.
"""

import logging
from typing import Optional, Dict, List
from datetime import datetime, timedelta
from pathlib import Path
import json

import pandas as pd

try:
    import yfinance as yf
except ImportError:
    yf = None


class YFinanceClient:
    """Client for Yahoo Finance FX data via yfinance."""
    
    # Pair mapping: OANDA format -> Yahoo Finance format
    PAIR_MAPPING = {
        'EUR_USD': 'EURUSD=X',
        'GBP_USD': 'GBPUSD=X',
        'USD_JPY': 'USDJPY=X',
        'USD_CHF': 'USDCHF=X',
        'AUD_USD': 'AUDUSD=X',
        'NZD_USD': 'NZDUSD=X',
        'USD_CAD': 'USDCAD=X',
        'EUR_GBP': 'EURGBP=X',
        'EUR_JPY': 'EURJPY=X',
        'GBP_JPY': 'GBPJPY=X',
    }
    
    # Interval mapping: Common timeframes
    INTERVAL_MAPPING = {
        'M1': '1m',
        'M5': '5m',
        'M15': '15m',
        'M30': '30m',
        'H1': '1h',
        'H4': '4h',  # Note: yfinance doesn't support 4h directly
        'D1': '1d',
        'W1': '1wk',
        'MN': '1mo',
    }
    
    def __init__(
        self,
        logger: Optional[logging.Logger] = None,
        cache_dir: Optional[str] = None,
        cache_hours: int = 24
    ):
        """
        Initialize yfinance client.
        
        Args:
            logger: Logger instance
            cache_dir: Directory for caching data
            cache_hours: Cache validity in hours
        """
        self.logger = logger or logging.getLogger('yfinance')
        self.cache_dir = Path(cache_dir or 'data/cache/yfinance')
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_hours = cache_hours
        
        if yf is None:
            self.logger.warning("yfinance not installed. Run: pip install yfinance")
    
    def _convert_pair(self, pair: str) -> str:
        """Convert OANDA pair format to Yahoo Finance format."""
        if pair in self.PAIR_MAPPING:
            return self.PAIR_MAPPING[pair]
        # Try direct conversion: EUR_USD -> EURUSD=X
        return pair.replace('_', '') + '=X'
    
    def _convert_interval(self, interval: str) -> str:
        """Convert common interval to yfinance format."""
        return self.INTERVAL_MAPPING.get(interval, interval)
    
    def _get_cache_path(self, pair: str, interval: str, period: str) -> Path:
        """Get cache file path."""
        pair_clean = pair.replace('_', '').replace('=X', '')
        return self.cache_dir / f"{pair_clean}_{interval}_{period}.json"
    
    def _is_cache_valid(self, cache_path: Path) -> bool:
        """Check if cached data is still valid."""
        if not cache_path.exists():
            return False
        mod_time = datetime.fromtimestamp(cache_path.stat().st_mtime)
        age_hours = (datetime.now() - mod_time).total_seconds() / 3600
        return age_hours < self.cache_hours
    
    def _load_from_cache(self, cache_path: Path) -> Optional[pd.DataFrame]:
        """Load data from cache."""
        try:
            with open(cache_path, 'r') as f:
                data = json.load(f)
            df = pd.DataFrame(data)
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df.set_index('timestamp', inplace=True)
            self.logger.debug(f"Loaded from cache: {cache_path.name}")
            return df
        except Exception as e:
            self.logger.warning(f"Cache load failed: {e}")
            return None
    
    def _save_to_cache(self, df: pd.DataFrame, cache_path: Path):
        """Save dataframe to cache."""
        try:
            df_save = df.reset_index()
            df_save['timestamp'] = df_save['timestamp'].astype(str)
            with open(cache_path, 'w') as f:
                json.dump(df_save.to_dict('records'), f)
            self.logger.debug(f"Saved to cache: {cache_path.name}")
        except Exception as e:
            self.logger.warning(f"Cache save failed: {e}")
    
    def get_historical_data(
        self,
        pair: str,
        interval: str = 'H1',
        period: str = '1mo',
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        use_cache: bool = True
    ) -> Optional[pd.DataFrame]:
        """
        Get historical OHLCV data for a forex pair.
        
        Args:
            pair: Currency pair in OANDA format (e.g., 'EUR_USD')
            interval: Time interval (M1, M5, M15, M30, H1, D1, W1)
            period: Data period (1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max)
            start_date: Start date (YYYY-MM-DD) - overrides period
            end_date: End date (YYYY-MM-DD)
            use_cache: Whether to use cached data
        
        Returns:
            DataFrame with columns: open, high, low, close, volume
        """
        if yf is None:
            self.logger.error("yfinance not available")
            return None
        
        # Check cache
        cache_path = self._get_cache_path(pair, interval, period)
        if use_cache and start_date is None and self._is_cache_valid(cache_path):
            cached = self._load_from_cache(cache_path)
            if cached is not None:
                return cached
        
        # Convert formats
        symbol = self._convert_pair(pair)
        yf_interval = self._convert_interval(interval)
        
        try:
            ticker = yf.Ticker(symbol)
            
            if start_date:
                df = ticker.history(
                    start=start_date,
                    end=end_date,
                    interval=yf_interval
                )
            else:
                df = ticker.history(
                    period=period,
                    interval=yf_interval
                )
            
            if df.empty:
                self.logger.warning(f"No data returned for {pair}")
                return None
            
            # Standardize column names
            df.columns = [c.lower() for c in df.columns]
            
            # Keep only OHLCV columns
            cols_to_keep = ['open', 'high', 'low', 'close', 'volume']
            df = df[[c for c in cols_to_keep if c in df.columns]]
            
            # Ensure index is named 'timestamp'
            df.index.name = 'timestamp'
            
            # Save to cache
            if use_cache and start_date is None:
                self._save_to_cache(df, cache_path)
            
            self.logger.info(
                f"✅ Fetched {pair} ({symbol}): {len(df)} candles "
                f"({df.index[0]} to {df.index[-1]})"
            )
            
            return df
            
        except Exception as e:
            self.logger.error(f"Error fetching {pair}: {e}")
            return None
    
    def get_recent_data(
        self,
        pair: str,
        days: int = 30,
        interval: str = 'H1'
    ) -> Optional[pd.DataFrame]:
        """
        Get recent historical data.
        
        Args:
            pair: Currency pair
            days: Number of days of data
            interval: Time interval
        
        Returns:
            DataFrame with OHLCV data
        """
        period_map = {
            1: '1d',
            5: '5d',
            7: '5d',
            30: '1mo',
            90: '3mo',
            180: '6mo',
            365: '1y',
        }
        
        # Find closest period
        period = '1mo'
        for d, p in sorted(period_map.items()):
            if days <= d:
                period = p
                break
        
        return self.get_historical_data(pair, interval=interval, period=period)
    
    def get_latest_price(self, pair: str) -> Optional[Dict]:
        """
        Get latest price for a pair.
        
        Args:
            pair: Currency pair
        
        Returns:
            Dict with bid, ask, spread (approximated from close)
        """
        if yf is None:
            return None
        
        symbol = self._convert_pair(pair)
        
        try:
            ticker = yf.Ticker(symbol)
            info = ticker.fast_info
            
            price = info.get('lastPrice') or info.get('regularMarketPrice')
            if price is None:
                # Fallback: get latest candle
                df = self.get_historical_data(pair, interval='M5', period='1d', use_cache=False)
                if df is not None and not df.empty:
                    price = float(df['close'].iloc[-1])
            
            if price:
                # Estimate spread (forex spreads are typically 0.5-2 pips)
                pip_size = 0.01 if 'JPY' in pair else 0.0001
                spread_pips = 1.0  # Approximate
                spread = spread_pips * pip_size
                
                return {
                    'bid': price - spread / 2,
                    'ask': price + spread / 2,
                    'spread': spread,
                    'mid': price,
                    'time': datetime.now().isoformat()
                }
            
            return None
            
        except Exception as e:
            self.logger.error(f"Error getting price for {pair}: {e}")
            return None
    
    def get_multiple_pairs(
        self,
        pairs: List[str],
        interval: str = 'H1',
        period: str = '1mo'
    ) -> Dict[str, pd.DataFrame]:
        """
        Get historical data for multiple pairs.
        
        Args:
            pairs: List of currency pairs
            interval: Time interval
            period: Data period
        
        Returns:
            Dict mapping pair names to DataFrames
        """
        results = {}
        for pair in pairs:
            df = self.get_historical_data(pair, interval=interval, period=period)
            if df is not None:
                results[pair] = df
        return results
    
    def get_daily_summary(self, pair: str, days: int = 5) -> Optional[List[Dict]]:
        """
        Get daily price summary for a pair.
        
        Args:
            pair: Currency pair
            days: Number of days
        
        Returns:
            List of daily summaries
        """
        df = self.get_historical_data(pair, interval='D1', period=f'{days}d')
        
        if df is None or df.empty:
            return None
        
        summaries = []
        for idx, row in df.iterrows():
            summaries.append({
                'date': idx.strftime('%Y-%m-%d'),
                'open': float(row['open']),
                'high': float(row['high']),
                'low': float(row['low']),
                'close': float(row['close']),
                'change': float(row['close'] - row['open']),
                'change_pct': float((row['close'] - row['open']) / row['open'] * 100)
            })
        
        return summaries
