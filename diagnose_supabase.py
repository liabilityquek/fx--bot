"""Diagnostic script for Supabase connection issues.

Run this to identify the exact problem with your Supabase setup.
"""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from monitoring.supabase_logger import create_supabase_logger


def diagnose_supabase():
    """Diagnose Supabase connection and permission issues."""

    print("Supabase Connection Diagnostics")
    print("=" * 50)

    # Check environment variables
    print("\n1. Checking environment variables...")
    supabase_url = os.environ.get('SUPABASE_URL', '').strip()
    supabase_key = os.environ.get('SUPABASE_KEY', '').strip()

    if not supabase_url:
        print("[ERROR] SUPABASE_URL not set")
        return False
    else:
        print(f"[OK] SUPABASE_URL: {supabase_url[:30]}...")

    if not supabase_key:
        print("[ERROR] SUPABASE_KEY not set")
        return False
    else:
        key_type = "service_role" if "service_role" in supabase_key else "anon/public"
        print(f"[OK] SUPABASE_KEY ({key_type}): {supabase_key[:20]}...")

    # Create logger
    print("\n2. Creating Supabase client...")
    try:
        logger = create_supabase_logger()
        if not logger:
            print("[ERROR] Could not create Supabase logger")
            return False
        print("[OK] Supabase client created")
    except Exception as e:
        print(f"[ERROR] Failed to create client: {e}")
        return False

    # Test basic connection
    print("\n3. Testing basic connection...")
    try:
        # Try to access the client directly
        client = logger._client
        print(f"[OK] Client accessible, type: {type(client)}")
    except Exception as e:
        print(f"[ERROR] Client access failed: {e}")
        return False

    # Test table access
    print("\n4. Testing table access...")
    try:
        # Try to query the trades table
        response = client.table('trades').select('*').limit(1).execute()
        print(f"[OK] Table access successful")
        print(f"     Response data: {response.data}")
    except Exception as e:
        print(f"[ERROR] Table access failed: {e}")
        print("\n   Possible causes:")
        print("   - Table 'trades' doesn't exist")
        print("   - RLS policy blocking SELECT")
        print("   - Invalid credentials")
        print("   - Supabase URL is incorrect")
        return False

    # Test insert with minimal data
    print("\n5. Testing insert with minimal data...")
    test_data = {
        'trade_id': 'diagnostic_test',
        'pair': 'EUR/USD',
        'direction': 'BUY'
    }

    try:
        response = client.table('trades').insert(test_data).execute()
        print(f"[OK] Insert successful")
        print(f"     Response: {response.data}")
    except Exception as e:
        print(f"[ERROR] Insert failed: {e}")
        print("\n   Possible causes:")
        print("   - RLS policy blocking INSERT")
        print("   - Missing required columns")
        print("   - Invalid data types")
        return False

    print("\n" + "=" * 50)
    print("[SUCCESS] All diagnostics passed")
    print("=" * 50)
    return True


if __name__ == '__main__':
    success = diagnose_supabase()
    sys.exit(0 if success else 1)
