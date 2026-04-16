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


def get_pip_value(pair: str, position_size: float = 10000) -> float:
    """
    Calculate pip value in account currency (USD) for a given position size.
    
    Args:
        pair: Trading pair (e.g., 'EUR_USD')
        position_size: Position size in units (default 10,000 = 0.1 lot)
    
    Returns:
        Pip value in USD
    """
    info = PAIR_INFO.get(pair)
    if not info:
        raise ValueError(f"Unknown pair: {pair}")
    
    # For pairs quoted in USD (XXX_USD), pip value is straightforward
    if info['quote_currency'] == 'USD':
        return info['pip_value'] * position_size
    
    # For USD_XXX pairs, need to account for current exchange rate
    # Simplified: use typical values
    if pair == 'USD_JPY':
        # 1 pip = 0.01 JPY, for 10k units = 100 JPY
        # At ~150 USD/JPY, 100 JPY ≈ $0.67
        return (info['pip_value'] * position_size) / 150  # Approximate
    
    if pair == 'USD_CHF':
        # Similar calculation
        return (info['pip_value'] * position_size) / 0.9  # Approximate
    
    return info['pip_value'] * position_size


def calculate_position_size_for_risk(
    pair: str,
    account_balance: float,
    risk_percent: float,
    stop_loss_pips: int
) -> int:
    """
    Calculate position size based on account balance and risk parameters.
    
    Args:
        pair: Trading pair
        account_balance: Current account balance in USD
        risk_percent: Percentage of account to risk (e.g., 0.02 for 2%)
        stop_loss_pips: Stop loss distance in pips
    
    Returns:
        Position size in units
    """
    risk_amount = account_balance * risk_percent
    pip_value_per_unit = get_pip_value(pair, 1)
    position_size = risk_amount / (stop_loss_pips * pip_value_per_unit)
    
    return int(position_size)
