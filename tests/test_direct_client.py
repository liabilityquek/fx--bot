"""Test direct Supabase client usage to identify the issue."""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from monitoring.supabase_logger import create_supabase_logger


def test_direct_client():
    """Test direct client usage."""

    print("Testing direct Supabase client...")
    print("=" * 50)

    logger = create_supabase_logger()
    if not logger:
        print("[ERROR] Could not create Supabase logger")
        return False

    client = logger._client

    # Check client attributes
    print(f"\n1. Client type: {type(client)}")
    print(f"   Available attributes: {[attr for attr in dir(client) if not attr.startswith('_')]}")

    # Try different table access patterns
    print("\n2. Testing table access patterns...")

    # Pattern 1: Standard table access
    try:
        response = client.table('trades').select('*').limit(1).execute()
        print(f"[OK] Standard pattern worked: {response.data}")
        return True
    except Exception as e:
        print(f"[FAILED] Standard pattern: {str(e)[:80]}...")

    # Pattern 2: With schema prefix
    try:
        response = client.schema('public').table('trades').select('*').limit(1).execute()
        print(f"[OK] Schema prefix pattern worked: {response.data}")
        return True
    except Exception as e:
        print(f"[FAILED] Schema prefix pattern: {str(e)[:80]}...")

    print("\n" + "=" * 50)
    print("If all patterns failed, check:")
    print("1. Supabase project URL is correct")
    print("2. API key has proper permissions")
    print("3. Table 'trades' exists in 'public' schema")
    print("4. RLS policies allow access (if using anon key)")

    return False


if __name__ == '__main__':
    success = test_direct_client()
    sys.exit(0 if success else 1)
