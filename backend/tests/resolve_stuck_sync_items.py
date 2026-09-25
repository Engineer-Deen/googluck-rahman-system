"""
Safely inspect and, only with your explicit confirmation, remove
permanently-stuck sync_outbox rows (status 'pending' or 'needs_review').

This is READ-ONLY until you type "yes" at the confirmation prompt for
each item. Nothing is deleted before that.

Usage (app must be fully closed first -- check Task Manager for a
lingering goodluck-backend.exe):
    python resolve_stuck_sync_items.py "C:\\path\\to\\glr_local.sqlite"
"""
import sqlite3
import sys
import json

if len(sys.argv) != 2:
    print("Usage: python resolve_stuck_sync_items.py <path-to-sqlite-file>")
    sys.exit(1)

path = sys.argv[1]
con = sqlite3.connect(path)
con.row_factory = sqlite3.Row
cur = con.cursor()

cur.execute("""
    SELECT id, table_name, record_id, status, attempt_count, last_error, payload_json
    FROM sync_outbox
    WHERE status IN ('pending', 'needs_review')
    ORDER BY id ASC
""")
stuck = cur.fetchall()

if not stuck:
    print("Nothing stuck -- sync_outbox has no pending/needs_review rows.")
    sys.exit(0)

for row in stuck:
    print("=" * 70)
    print(f"outbox id:     {row['id']}")
    print(f"table:         {row['table_name']}")
    print(f"record_id:     {row['record_id']}")
    print(f"status:        {row['status']}")
    print(f"attempts:      {row['attempt_count']}")
    print(f"last_error:    {row['last_error']}")

    payload = {}
    try:
        payload = json.loads(row["payload_json"])
    except Exception:
        pass
    print(f"customer_name: {payload.get('customer_name')}")
    print(f"total (payload amount_paid): {payload.get('amount_paid')}")

    sale_row = None
    if row["table_name"] == "sales":
        cur.execute(
            "SELECT id, customer_name, total_amount, created_at, voided_at FROM sales WHERE id = ?",
            (row["record_id"],),
        )
        sale_row = cur.fetchone()

    if sale_row:
        print("--- Matching row still exists in your LOCAL sales table: ---")
        print(f"  id:            {sale_row['id']}")
        print(f"  customer_name: {sale_row['customer_name']}")
        print(f"  total_amount:  {sale_row['total_amount']}")
        print(f"  created_at:    {sale_row['created_at']}")
        print(f"  voided_at:     {sale_row['voided_at']}")
    else:
        print("--- No matching row in your local sales table. This outbox ---")
        print("--- entry is an orphan: deleting it affects NOTHING visible. ---")

    print()
    answer = input(
        "Delete this outbox entry (does NOT touch your Transaction History "
        "or any dashboard numbers -- it only stops the sync retry loop)? "
        "Type 'yes' to delete, anything else to skip: "
    ).strip().lower()

    if answer == "yes":
        cur.execute("DELETE FROM sync_outbox WHERE id = ?", (row["id"],))
        con.commit()
        print(f"Deleted outbox id {row['id']}.\n")

        if sale_row:
            purge = input(
                "This item also has a matching row in your local sales table "
                f"(customer: '{sale_row['customer_name']}'). If this was test/"
                "junk data (not a real transaction), you can remove it from "
                "Transaction History too. Type 'yes' to also delete the sale "
                "record itself, anything else to leave it: "
            ).strip().lower()
            if purge == "yes":
                cur.execute("DELETE FROM sale_items WHERE sale_id = ?", (row["record_id"],))
                cur.execute("DELETE FROM sale_payments WHERE sale_id = ?", (row["record_id"],))
                cur.execute("DELETE FROM stock_movements WHERE reference_id = ?", (row["record_id"],))
                cur.execute("DELETE FROM sales WHERE id = ?", (row["record_id"],))
                con.commit()
                print(f"Deleted sale {row['record_id']} and its items/payments/stock movements.\n")
    else:
        print("Skipped.\n")

print("Done.")
