"""Test with all likely required fields."""

import os
import sys
from datetime import datetime

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.monitoring.supabase_logger import create_supabase_logger


def test_complete():
    """Test insert with complete field set."""

    print("Testing insert with complete field set...")
    print("=" * 50)

    logger = create_supabase_logger()
    if not logger:
        print("[ERROR] Could not create Supabase logger")
        return False

    client = logger._client

    # Test data with likely required fields
    test_data = {
        'trade_id': f"test_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        'pair': 'EUR/USD',
        'side': 'BUY',
        'units': 1000,
        'entry_price': 1.0850,
        'stop_loss': 1.0800,
        'take_profit': 1.0950,
    }

    print(f"\n1. Testing insert with complete fields...")
    try:
        response = client.table('trades').insert(test_data).execute()
        print(f"[OK] Insert succeeded: {response.data}")
        print(f"   Trade ID: {test_data['trade_id']}")
        return True
    except Exception as e:
        print(f"[FAILED] Insert failed: {e}")
        return False


if __name__ == '__main__':
    success = test_complete()
    sys.exit(0 if success else 1)
