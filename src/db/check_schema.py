"""Check the schema of the trades table."""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from monitoring.supabase_logger import create_supabase_logger


def check_schema():
    """Check the schema of the trades table."""

    print("Checking trades table schema...")
    print("=" * 50)

    logger = create_supabase_logger()
    if not logger:
        print("[ERROR] Could not create Supabase logger")
        return False

    client = logger._client

    # Get a sample record to see the schema
    print("\n1. Getting sample data to infer schema...")
    try:
        response = client.table('trades').select('*').limit(1).execute()
        if response.data:
            print(f"[OK] Found {len(response.data)} existing record(s)")
            print(f"   Sample record: {response.data[0]}")
            print(f"   Available columns: {list(response.data[0].keys())}")
        else:
            print("[INFO] Table is empty - no existing records")
    except Exception as e:
        print(f"[ERROR] Failed to get sample: {e}")

    # Try to get table information via RPC or other methods
    print("\n2. Trying to get table schema...")
    try:
        # Try using the information schema (might not work with anon key)
        response = client.table('information_schema.columns').select('column_name,data_type').eq('table_name', 'trades').execute()
        if response.data:
            print(f"[OK] Found schema information:")
            for col in response.data:
                print(f"   - {col['column_name']}: {col['data_type']}")
        else:
            print("[INFO] Could not access information_schema (expected with anon key)")
    except Exception as e:
        print(f"[INFO] Information schema access failed (expected with anon key): {str(e)[:50]}...")

    # Test insert with different field combinations
    print("\n3. Testing insert with different field combinations...")

    # Try minimal insert
    test_cases = [
        {'trade_id': 'test_minimal'},
        {'trade_id': 'test_basic', 'pair': 'EUR/USD'},
        {'trade_id': 'test_full', 'pair': 'EUR/USD', 'entry_price': 1.0850},
    ]

    for i, test_data in enumerate(test_cases, 1):
        try:
            response = client.table('trades').insert(test_data).execute()
            print(f"[OK] Test case {i} succeeded: {test_data}")
            print(f"   Inserted record: {response.data}")
            return True
        except Exception as e:
            print(f"[FAILED] Test case {i}: {str(e)[:60]}...")

    print("\n" + "=" * 50)
    print("[INFO] Could not determine schema from tests")
    print("Please check your Supabase dashboard:")
    print("1. Go to Table Editor")
    print("2. Select 'trades' table")
    print("3. View the column structure")
    print("=" * 50)

    return False


if __name__ == '__main__':
    success = check_schema()
    sys.exit(0 if success else 1)
