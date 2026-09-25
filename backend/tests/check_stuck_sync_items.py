"""
Run this once the desktop app is fully closed, pointed at your local
device's SQLite file, to see exactly why the 2 stuck items are being
rejected by the central server.

Usage (from a terminal / PowerShell, with Python installed):
    python check_stuck_sync_items.py "C:\path\to\your\local.sqlite"

If you're not sure where the file is, look inside the app's install
folder or its per-user data folder (often under
%APPDATA%\<app-name>\ or %LOCALAPPDATA%\<app-name>\) for a .sqlite file.
"""
import sqlite3
import sys
import json

if len(sys.argv) != 2:
    print("Usage: python check_stuck_sync_items.py <path-to-sqlite-file>")
    sys.exit(1)

path = sys.argv[1]
con = sqlite3.connect(path)
con.row_factory = sqlite3.Row
cur = con.cursor()

cur.execute("""
    SELECT id, table_name, record_id, status, attempt_count,
           last_attempt_at, last_error, payload_json
    FROM sync_outbox
    ORDER BY created_at ASC
""")
rows = cur.fetchall()

if not rows:
    print("No rows in sync_outbox at all -- nothing pending or needing review.")
else:
    for r in rows:
        print("=" * 70)
        print(f"outbox id:      {r['id']}")
        print(f"table:          {r['table_name']}")
        print(f"record_id:      {r['record_id']}")
        print(f"status:         {r['status']}")
        print(f"attempt_count:  {r['attempt_count']}")
        print(f"last_attempt:   {r['last_attempt_at']}")
        print(f"LAST ERROR:     {r['last_error']}")
        try:
            payload = json.loads(r["payload_json"])
            print(f"payload shop_id:    {payload.get('shop_id')}")
            print(f"payload device_id:  {payload.get('device_id')}")
            print(f"payload customer:   {payload.get('customer_name')}")
        except Exception as e:
            print(f"(could not parse payload_json: {e})")

print("=" * 70)
print(f"Total rows: {len(rows)}")
