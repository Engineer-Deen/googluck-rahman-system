import sqlite3

sources = [
    ('local', r'C:\Users\Engr. Deen\Desktop\goodluck-rahman-system\backend\instance\glr_local.sqlite'),
    ('central', r'C:\Users\Engr. Deen\Desktop\goodluck-rahman-system\backend\instance\glr_central.sqlite'),
]

for label, db in sources:
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    print(f'=== {label.upper()} TABLES ===')
    print(cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall())
    print(f'=== {label.upper()} DEVICES ===')
    for row in cur.execute('SELECT * FROM devices ORDER BY registered_at'):
        print(row)
    print(f'=== {label.upper()} SHOPS ===')
    for row in cur.execute('SELECT * FROM shops ORDER BY id'):
        print(row)
    print(f'=== {label.upper()} STAFF ===')
    for row in cur.execute('SELECT * FROM staff ORDER BY id'):
        print(row)
    print(f'=== {label.upper()} SALES ===')
    for row in cur.execute('SELECT * FROM sales ORDER BY created_at'):
        print(row)
    print(f'=== {label.upper()} STOCK_MOVEMENTS ===')
    for row in cur.execute('SELECT * FROM stock_movements ORDER BY created_at'):
        print(row)
    print(f'=== {label.upper()} SYNC_OUTBOX ===')
    for row in cur.execute('SELECT * FROM sync_outbox ORDER BY created_at'):
        print(row)
    conn.close()
