"""Check the actual Supabase URL being used."""

import os
import sys

# Add src to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

from monitoring.supabase_logger import create_supabase_logger


def check_url():
    """Check the actual URL being used."""

    print("Checking Supabase URL configuration...")
    print("=" * 50)

    logger = create_supabase_logger()
    if not logger:
        print("[ERROR] Could not create Supabase logger")
        return False

    client = logger._client

    # Check the URL
    print(f"\n1. Supabase URL: {client.supabase_url}")
    print(f"   REST URL: {client.rest_url}")
    print(f"   Auth URL: {client.auth_url}")

    # Check if URL ends correctly
    if client.supabase_url.endswith('/'):
        print("[WARNING] URL ends with slash - this might cause issues")
    else:
        print("[OK] URL format looks correct")

    # Try to construct the table URL manually
    print(f"\n2. Manual URL construction test...")
    table_url = f"{client.rest_url}/trades"
    print(f"   Expected table URL: {table_url}")

    # Test if the REST URL is accessible
    try:
        import httpx
        response = httpx.get(client.rest_url, headers={
            'apikey': client.supabase_key,
            'Authorization': f'Bearer {client.supabase_key}'
        })
        print(f"[OK] REST URL accessible: {response.status_code}")
        print(f"   Response: {response.text[:200]}...")
    except Exception as e:
        print(f"[ERROR] REST URL failed: {e}")

    # Test the table URL directly
    try:
        import httpx
        response = httpx.get(table_url, headers={
            'apikey': client.supabase_key,
            'Authorization': f'Bearer {client.supabase_key}'
        })
        print(f"[OK] Table URL accessible: {response.status_code}")
        print(f"   Response: {response.text[:200]}...")
    except Exception as e:
        print(f"[ERROR] Table URL failed: {e}")

    return True


if __name__ == '__main__':
    check_url()
