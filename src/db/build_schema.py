"""Build up the schema incrementally to find all required fields."""

import os
import sys
from datetime import datetime

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from monitoring.supabase_logger import create_supabase_logger


def build_schema():
    """Build up the schema incrementally."""

    print("Building schema incrementally...")
    print("=" * 50)

    logger = create_supabase_logger()
    if not logger:
        print("[ERROR] Could not create Supabase logger")
        return False

    client = logger._client

    # Start with minimal required fields
    base_data = {
        'trade_id': f"test_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
        'pair': 'EUR/USD',
        'side': 'BUY'
    }

    # Fields to test incrementally
    test_fields = [
        ('entry_price', 1.0850),
        ('stop_loss', 1.0800),
        ('take_profit', 1.0950),
        ('entry_time', datetime.now().isoformat()),
        ('close_time', datetime.now().isoformat()),
        ('close_price', 1.0900),
        ('pnl', 50.0),
        ('pnl_pips', 50.0),
        ('close_reason', 'TP'),
        ('confidence', 0.75),
        ('setup_type', 'BREAKOUT'),
        ('reviewer_verdict', 'APPROVED'),
        ('direction', 'BUY'),  # Test if this field exists
    ]

    print("\n1. Testing base insert with minimal fields...")
    try:
        response = client.table('trades').insert(base_data).execute()
        print(f"[OK] Base insert succeeded: {response.data}")
        print(f"   Trade ID: {base_data['trade_id']}")
        return True
    except Exception as e:
        print(f"[FAILED] Base insert failed: {e}")

    # Try adding fields one by one
    print("\n2. Testing incremental field additions...")
    current_data = base_data.copy()

    for field_name, field_value in test_fields:
        current_data[field_name] = field_value
        current_data['trade_id'] = f"test_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{field_name}"

        try:
            response = client.table('trades').insert(current_data).execute()
            print(f"[OK] Field '{field_name}' accepted")
        except Exception as e:
            print(f"[FAILED] Field '{field_name}' rejected: {str(e)[:60]}...")
            # Remove the failed field and continue
            del current_data[field_name]

    print("\n" + "=" * 50)
    print("Final working schema:")
    for key, value in current_data.items():
        print(f"   - {key}: {type(value).__name__}")
    print("=" * 50)

    return False


if __name__ == '__main__':
    build_schema()
