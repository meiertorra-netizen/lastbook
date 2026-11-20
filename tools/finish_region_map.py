"""Finish a map set built outside the US, so setup and the app take it like a state map (docs/SETUP-API.md, "State maps").

  python3 tools/finish_region_map.py OUT_FOLDER ES-CT --title Catalonia \
      --credit "© OpenStreetMap contributors · Copernicus DEM © DLR, Airbus, ESA · Addresses: scne.es (CC BY 4.0) via Overture"

Run it after tools/build_osm_region.py, tools/build_terrain.py --src/--out and tools/build_addresses_es.py have written
OUT/derived/geo/. It adds what the app expects next to them (an empty map.sqlite, socal-cells.json), stores the credit
line, the data date and one line per data source in national.sqlite's meta (server.py region_meta, shown by web/map.js
instead of the US credits), and writes map-set.json with every file's size, as build_packs.py does for a state.
With --zip DIR it also writes Lastbook-Map-<code>.zip (the map set, laid out as setup's map_member allows) and
Lastbook-Addresses-<code>.zip (the address index, which setup installs as its own item), with their SHA-256 and sizes
for the manifest's "maps" and "address_indexes" entries.
"""
import argparse, hashlib, json, sqlite3, time, zipfile
from pathlib import Path

LABELS = {   # file -> what it is, for the panel beside the map; the source comes from the file's own meta
    'national.sqlite': ('Streets, places, water and help points', 'Street detail stops at the edge of this area.'),
    'buildings.sqlite': ('Building outlines', 'Shown from street level zoom'),
    'terrain.sqlite': ('Elevation', 'Shaded relief and contour lines'),
    'places.sqlite': ('Place names', 'Used by "Find an address, town or ZIP"'),
}


def meta(path):
    try:
        db = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            return dict(db.execute('SELECT key,value FROM meta').fetchall())
        finally:
            db.close()
    except sqlite3.Error:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('out')
    ap.add_argument('code', help='the map id, e.g. ES-CT; it names map-set.json and Lastbook-Map-<code>.zip')
    ap.add_argument('--title', required=True)
    ap.add_argument('--credit', required=True, help='the credit line shown on the map')
    ap.add_argument('--zip', help='folder to write the two zips to')
    ap.add_argument('--units', choices=('metric', 'imperial'), default='metric', help='units the map shows first (default: metric)')
    a = ap.parse_args()
    out = Path(a.out)
    geo = out / 'derived/geo'
    m = sqlite3.connect(geo / 'map.sqlite')       # server.py draws nothing without one; the region's data is in national.sqlite
    m.executescript('''CREATE TABLE IF NOT EXISTS feat(id INTEGER PRIMARY KEY, layer TEXT, cls TEXT, name TEXT, minz INTEGER, geom BLOB);
      CREATE VIRTUAL TABLE IF NOT EXISTS feat_idx USING rtree(id, minx, maxx, miny, maxy, minz, maxz);
      CREATE TABLE IF NOT EXISTS poi(id INTEGER PRIMARY KEY, cls TEXT, name TEXT, lon REAL, lat REAL);
      CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);''')
    m.commit()
    m.close()
    (geo / 'socal-cells.json').write_text('[]', encoding='utf-8')

    national = meta(geo / 'national.sqlite')
    datasets = []
    for name, (label, status) in LABELS.items():
        source = meta(geo / name).get('source') if (geo / name).exists() else None
        if name == 'places.sqlite' and (geo / name).exists():
            source = national.get('source')
        if source:
            datasets.append({'label': f'{label} ({source})', 'status': status})
    for index in sorted((geo / 'addresses').glob('*.sqlite')):
        source = meta(index).get('source')
        if source:
            datasets.append({'label': f'Street addresses ({source})', 'status': f'{int(meta(index).get("count", 0)):,} addresses'})
    db = sqlite3.connect(geo / 'national.sqlite')
    db.executemany('INSERT OR REPLACE INTO meta VALUES (?,?)', [('credit', a.credit), ('datasets', json.dumps(datasets)),
                                                                ('region', a.code), ('title', a.title), ('units', a.units)])
    db.commit()
    db.close()

    for f in sorted(geo.glob('*.sqlite')):        # fold any write-ahead log back in: a zip carries the database file alone
        d = sqlite3.connect(f)
        d.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        d.execute('PRAGMA journal_mode=DELETE')
        d.close()
    # Only what a map zip may hold (engine/setup/install.py map_member): the app's own caches and indexes, and the
    # address index, which setup installs as its own item, stay out.
    fixed = {'derived/geo/national-status.json', 'derived/geo/socal-cells.json'}
    size = {f.relative_to(out).as_posix(): f.stat().st_size for f in sorted(geo.iterdir())
            if f.is_file() and (f.suffix == '.sqlite' or f.relative_to(out).as_posix() in fixed)}
    (out / 'map-set.json').write_text(json.dumps({'name': a.code, 'title': a.title, 'files': size, 'bytes': sum(size.values()),
                                                 'built': time.strftime('%Y-%m-%d')}, indent=1), encoding='utf-8')
    print(json.dumps({'map': a.code, 'bytes': sum(size.values()), 'datasets': datasets}, indent=1, ensure_ascii=False))
    if a.zip:
        zips = Path(a.zip)
        zips.mkdir(parents=True, exist_ok=True)
        made = {}
        index = geo / 'addresses' / f'{a.code}.sqlite'
        for name, members in ((f'Lastbook-Map-{a.code}.zip', ['map-set.json', *size]),
                              (f'Lastbook-Addresses-{a.code}.zip', [index.relative_to(out).as_posix()] if index.exists() else [])):
            if not members:
                continue
            path = zips / name
            with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                for rel in members:
                    z.write(out / rel, rel)
            digest = hashlib.sha256()
            with open(path, 'rb') as f:
                for block in iter(lambda: f.read(1 << 20), b''):
                    digest.update(block)
            made[name] = {'size': path.stat().st_size, 'sha256': digest.hexdigest(),
                          'installed_bytes': sum((out / rel).stat().st_size for rel in members)}
        print(json.dumps(made, indent=1))


if __name__ == '__main__':
    main()
