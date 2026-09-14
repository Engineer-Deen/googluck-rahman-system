import sqlite3
from pathlib import Path

base = Path(r'C:\Users\Engr. Deen\Desktop\goodluck-rahman-system\backend\instance')

for label, db_path in [('local', base / 'glr_local.sqlite'), ('central', base / 'glr_central.sqlite')]:
    print(f'=== {label.upper()} DATABASE: {db_path} ===')
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    tables = [row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print('tables', tables)
    for table in ['devices', 'shops', 'staff', 'sales', 'stock_movements', 'sync_outbox']:
        print(f'--- {table} ---')
        try:
            rows = cur.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
            for row in rows:
                print(dict(row))
        except Exception as exc:
            print('ERROR', exc)
    conn.close()
