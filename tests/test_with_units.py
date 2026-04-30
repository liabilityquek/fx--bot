"""Test with units field included."""

import os
import sys
from datetime import datetime

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from monitoring.supabase_logger import create_supabase_logger


def test_with_units():
    """Test insert with units field."""

    print("Testing insert with units field...")
    print("=" * 50)

    logger = create_supabase_logger()
    if not logger:
        print("[ERROR] Could not create Supabase logger")
        return False

    client = logger._client

    # Test data with units field
    test_data = {
        'trade_id': f"test_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        'pair': 'EUR/USD',
        'side': 'BUY',
        'units': 1000  # Added units field
    }

    print(f"\n1. Testing insert with units field...")
    try:
        response = client.table('trades').insert(test_data).execute()
        print(f"[OK] Insert succeeded: {response.data}")
        print(f"   Trade ID: {test_data['trade_id']}")
        return True
    except Exception as e:
        print(f"[FAILED] Insert failed: {e}")
        return False


if __name__ == '__main__':
    success = test_with_units()
    sys.exit(0 if success else 1)
