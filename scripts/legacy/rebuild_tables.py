"""Rebuild events/statistics parquet from the raw cache with pyarrow-safe settings
(write_statistics=False sidesteps the 'Repetition level histogram size mismatch'
read bug). Data source of truth is data/apifootball/raw/*.json - always safe."""
import glob, json, re, sys, importlib.util
from pathlib import Path
import pandas as pd, pyarrow as pa, pyarrow.parquet as pq

OUT = Path("data/apifootball"); RAW = OUT / "raw"
spec = importlib.util.spec_from_file_location("dl", "scripts/download_apifootball.py")
dl = importlib.util.module_from_spec(spec); spec.loader.exec_module(dl)

# fixture_id -> join keys (fixtures.parquet is clean/readable)
fx = pq.read_table(OUT / "fixtures.parquet", use_threads=False).to_pandas()
keys = {int(r.fixture_id): {"fixture_id": int(r.fixture_id), "LeagueCode": r.LeagueCode,
        "Season": (int(r.Season) if pd.notna(r.Season) else None), "Date": r.Date}
        for r in fx.itertuples()}
fid_re = re.compile(r"fixture-(\d+)")

def rebuild_streamed(prefix, parse, name, schema, chunk=400_000):
    files = glob.glob(str(RAW / f"{prefix}__*.json"))
    writer = pq.ParquetWriter(OUT / f"{name}.parquet", schema, version="2.6")
    buf, total, skipped = [], 0, 0
    def flush():
        nonlocal buf, total
        if not buf: return
        tbl = pa.Table.from_pylist(buf, schema=schema)
        writer.write_table(tbl); total += len(buf); buf.clear()
    for f in files:
        m = fid_re.search(f)
        k = keys.get(int(m.group(1))) if m else None
        if not k: skipped += 1; continue
        try: body = json.loads(open(f).read())
        except Exception: skipped += 1; continue
        buf.extend(parse(k, body) or [])
        if len(buf) >= chunk: flush()
    flush(); writer.close()
    print(f"  {name}: {total:,} rows from {len(files):,} files ({skipped:,} skipped) -> {name}.parquet")
    chk = pq.read_table(OUT / f"{name}.parquet", use_threads=False)
    print(f"  {name}: re-read OK, {chk.num_rows:,} rows, {chk.num_columns} cols")

# events: fixed schema
ev_schema = pa.schema([("fixture_id", pa.int64()), ("LeagueCode", pa.string()),
    ("Season", pa.int64()), ("Date", pa.string()), ("minute", pa.int64()),
    ("minute_extra", pa.int64()), ("team", pa.string()), ("team_id", pa.int64()),
    ("type", pa.string()), ("detail", pa.string()), ("player", pa.string()),
    ("player_id", pa.int64()), ("assist", pa.string()), ("comments", pa.string())])
print("Rebuilding events...")
rebuild_streamed("fixtures_events", dl._parse_events, "events", ev_schema)

# statistics: variable stat columns -> collect in memory (small), unify, write
print("Rebuilding statistics...")
rows = []
for f in glob.glob(str(RAW / "fixtures_statistics__*.json")):
    m = fid_re.search(f); k = keys.get(int(m.group(1))) if m else None
    if not k: continue
    try: body = json.loads(open(f).read())
    except Exception: continue
    rows.extend(dl._parse_statistics(k, body) or [])
sdf = pd.DataFrame(rows)
# clean dtypes: ids/season int, everything else left; write_statistics=False for safe read
for c in ["fixture_id","team_id","Season"]:
    if c in sdf: sdf[c] = sdf[c].astype("Int64")
tbl = pa.Table.from_pandas(sdf.convert_dtypes(), preserve_index=False)
pq.write_table(tbl, OUT / "statistics.parquet", write_statistics=False, version="2.6")
chk = pq.read_table(OUT / "statistics.parquet", use_threads=False)
print(f"  statistics: {chk.num_rows:,} rows, {chk.num_columns} cols -> re-read OK")
print("\nDONE")
