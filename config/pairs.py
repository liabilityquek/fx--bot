"""FX pair definitions and metadata."""

# Trading pairs in OANDA format (underscore separator)
TRADING_PAIRS = [
    'EUR_USD',
    'GBP_USD',
    'USD_JPY',
    'USD_CHF',
    'AUD_USD'
]

# Pair information and pip values
PAIR_INFO = {
    'EUR_USD': {
        'name': 'Euro / US Dollar',
        'pip_decimal': 4,  # 0.0001
        'pip_value': 0.0001,
        'min_trade_units': 1,
        'typical_spread': 0.8,  # pips
        'max_leverage': 50,
        'base_currency': 'EUR',
        'quote_currency': 'USD'
    },
    'GBP_USD': {
        'name': 'British Pound / US Dollar',
        'pip_decimal': 4,
        'pip_value': 0.0001,
        'min_trade_units': 1,
        'typical_spread': 1.2,
        'max_leverage': 50,
        'base_currency': 'GBP',
        'quote_currency': 'USD'
    },
    'USD_JPY': {
        'name': 'US Dollar / Japanese Yen',
        'pip_decimal': 2,  # 0.01 for JPY pairs
        'pip_value': 0.01,
        'min_trade_units': 1,
        'typical_spread': 0.9,
        'max_leverage': 50,
        'base_currency': 'USD',
        'quote_currency': 'JPY'
    },
    'USD_CHF': {
        'name': 'US Dollar / Swiss Franc',
        'pip_decimal': 4,
        'pip_value': 0.0001,
        'min_trade_units': 1,
        'typical_spread': 1.5,
        'max_leverage': 50,
        'base_currency': 'USD',
        'quote_currency': 'CHF'
    },
    'AUD_USD': {
        'name': 'Australian Dollar / US Dollar',
        'pip_decimal': 4,
        'pip_value': 0.0001,
        'min_trade_units': 1,
        'typical_spread': 1.0,
        'max_leverage': 50,
        'base_currency': 'AUD',
        'quote_currency': 'USD'
    }
}


def get_pip_value(pair: str, position_size: float = 10000, current_price: float = None) -> float:
    """
    Calculate pip value in account currency (USD) for a given position size.

    Args:
        pair: Trading pair (e.g., 'EUR_USD')
        position_size: Position size in units (default 10,000 = 0.1 lot)
        current_price: Live market price — used for accurate conversion on USD_JPY, USD_CHF

    Returns:
        Pip value in USD
    """
    info = PAIR_INFO.get(pair)
    if not info:
        raise ValueError(f"Unknown pair: {pair}")

    # For pairs quoted in USD (XXX_USD), pip value is straightforward
    if info['quote_currency'] == 'USD':
        return info['pip_value'] * position_size

    # For USD_XXX pairs divide pip value (in quote currency) by the live rate to get USD
    if current_price and current_price > 0:
        return (info['pip_value'] * position_size) / current_price

    # Fallback approximations when no live price available
    if pair == 'USD_JPY':
        return (info['pip_value'] * position_size) / 150
    if pair == 'USD_CHF':
        return (info['pip_value'] * position_size) / 0.9

    return info['pip_value'] * position_size


# DEAD CODE — not called by the live pipeline and lacks live-price conversion for USD_JPY/USD_CHF.
# Use src/risk/position_sizer.py for all position sizing.
