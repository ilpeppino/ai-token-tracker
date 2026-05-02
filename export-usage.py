#!/usr/bin/env python3
import sqlite3
from pathlib import Path
from datetime import datetime

import pandas as pd

BASE = Path.home() / ".ai-token-tracker"
DB = BASE / "usage.sqlite"
OUT = BASE / "exports"
OUT.mkdir(parents=True, exist_ok=True)

stamp = datetime.now().strftime("%Y%m%d-%H%M%S")

conn = sqlite3.connect(DB)
df = pd.read_sql_query("SELECT * FROM usage_sessions ORDER BY date, timestamp", conn)
conn.close()

csv_path = OUT / f"usage-sessions-{stamp}.csv"
json_path = OUT / f"usage-sessions-{stamp}.json"

df.to_csv(csv_path, index=False)
df.to_json(json_path, orient="records", indent=2)

print(f"Exported CSV:  {csv_path}")
print(f"Exported JSON: {json_path}")
print(f"Rows: {len(df):,}")
