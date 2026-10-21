import os
import sqlite3

db = os.path.expanduser(r"~\.local\share\opencode\opencode.db")
con = sqlite3.connect(db)
tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
print("tables:", tables)
for t in tables:
    cols = [r[1] for r in con.execute(f"pragma table_info({t})")]
    print(f"  {t}: {cols}")
