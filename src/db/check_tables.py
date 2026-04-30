"""Check what tables exist in your Supabase project."""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from monitoring.supabase_logger import create_supabase_logger


def check_tables():
    """Check what tables exist in the database."""

    print("Checking Supabase tables...")
    print("=" * 50)

    logger = create_supabase_logger()
    if not logger:
        print("[ERROR] Could not create Supabase logger")
        return False

    client = logger._client

    # Try to list tables using the information schema
    print("\n1. Trying to query information_schema...")
    try:
        response = client.table('information_schema.tables').select('table_name').execute()
        print(f"[OK] Found {len(response.data)} tables:")
        for table in response.data:
            print(f"   - {table.get('table_name')}")
    except Exception as e:
        print(f"[ERROR] Failed to query information_schema: {e}")

    # Try direct table access with different names
    print("\n2. Testing common table names...")
    test_names = ['trades', 'Trades', 'TRADES', 'trade', 'Trade', 'fx_trades']

    for name in test_names:
        try:
            response = client.table(name).select('*').limit(1).execute()
            print(f"[OK] Table '{name}' exists and is accessible")
            print(f"     Sample data: {response.data}")
            return True
        except Exception as e:
            print(f"[FAILED] Table '{name}': {str(e)[:50]}...")

    print("\n" + "=" * 50)
    print("[ERROR] No accessible table found")
    print("=" * 50)
    print("\nNext steps:")
    print("1. Check your Supabase dashboard → Table Editor")
    print("2. Verify the exact table name (case-sensitive)")
    print("3. Create the 'trades' table if it doesn't exist")

    return False


if __name__ == '__main__':
    success = check_tables()
    sys.exit(0 if success else 1)
