"""Test Supabase operations with correct schema."""

import os
import sys
from datetime import datetime, timedelta

# Add src to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.monitoring.supabase_logger import create_supabase_logger


def test_supabase_operations():
    """Test insert, update, and query operations with correct schema."""

    print("Testing SupabaseTradeLogger operations...")
    print("-" * 50)

    # Create logger
    logger = create_supabase_logger()
    if not logger:
        print("[FAILED] Could not create Supabase logger")
        return False

    print("[OK] Supabase logger created successfully")

    # Test data with correct schema
    test_trade_id = f"test_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    test_data = {
        'trade_id': test_trade_id,
        'pair': 'EUR/USD',
        'direction': 'BUY',  # Will be normalized to 'side'
        'units': 1000,
        'entry_price': 1.0850,
        'stop_loss': 1.0800,
        'take_profit': 1.0950,
        'entry_time': datetime.now().isoformat(),  # Will be normalized to 'open_time'
        'close_time': (datetime.now() + timedelta(hours=2)).isoformat(),
        'close_price': 1.0900,
        'pnl': 50.0,  # Will be normalized to 'realized_pnl'
        'pnl_pips': 50.0,  # Will be normalized to 'pips_gained'
        'close_reason': 'TP',
        'confidence': 0.75,
        'setup_type': 'BREAKOUT',
        'reviewer_verdict': 'APPROVED'
    }

    # Test 1: Insert
    print(f"\n1. Testing insert_trade() with trade_id: {test_trade_id}")
    try:
        logger.insert_trade(test_data)
        # Verify insert
        retrieved = logger.get_trade(test_trade_id)
        if retrieved:
            print("[OK] Insert completed and verified")
        else:
            print("[FAILED] Insert appeared to complete but trade not found")
            return False
    except Exception as e:
        print(f"[FAILED] Insert failed: {e}")
        return False

    # Test 2: Get trade
    print(f"\n2. Testing get_trade() for trade_id: {test_trade_id}")
    retrieved = logger.get_trade(test_trade_id)
    if retrieved:
        print(f"[OK] Retrieved trade: {retrieved.get('pair')} {retrieved.get('side')}")
    else:
        print("[FAILED] Get trade failed or trade not found")
        return False

    # Test 3: Update trade
    print(f"\n3. Testing update_trade() - adding entry_reason")
    update_data = {'entry_reason': 'Test trade from verification script'}
    try:
        logger.update_trade(test_trade_id, update_data)
        print("[OK] Update completed")
    except Exception as e:
        print(f"[FAILED] Update failed: {e}")
        return False

    # Test 4: Verify update
    print(f"\n4. Verifying update by fetching trade again")
    updated = logger.get_trade(test_trade_id)
    if updated and updated.get('entry_reason') == 'Test trade from verification script':
        print("[OK] Update verified - entry_reason field present")
    else:
        print("[FAILED] Update verification failed")
        return False

    # Test 5: Get recent trades
    print(f"\n5. Testing get_recent_trades()")
    recent = logger.get_recent_trades(limit=5)
    if recent:
        print(f"[OK] Retrieved {len(recent)} recent trades")
        for trade in recent:
            print(f"   - {trade.get('trade_id')}: {trade.get('pair')} {trade.get('side')}")
    else:
        print("[WARNING] No recent trades found")

    print("\n" + "=" * 50)
    print("[OK] ALL TESTS PASSED")
    print("=" * 50)
    print(f"\nTest trade ID: {test_trade_id}")
    print("You can delete this test record from your Supabase dashboard")

    return True


if __name__ == '__main__':
    success = test_supabase_operations()
    sys.exit(0 if success else 1)
