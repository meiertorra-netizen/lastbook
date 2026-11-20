"""Street addresses for one Spanish region: derived/geo/addresses/ES-<code>.sqlite, from Overture Maps' address theme.

  derived/buildenv/bin/python tools/build_addresses_es.py CT OUT_FOLDER [--release 2026-11-18.0] [--parquet FILE]

Overture's Spanish rows come from one source, "scne.es, available under CC BY 4.0, distributed by OpenAddresses".
They carry the municipality in address_levels[1] and a five-digit postcode whose first two digits are the province,
so a region is cut exactly by its provinces' postcode prefixes (docs/REGIONS-ES.md, "Addresses in Overture, measured"),
never by a box, which would overlap its neighbours. --parquet reads a local copy instead of the public S3 bucket.

The table has the columns engine/address_search.py reads for US states (addr: num, street, norm, city, state, zip,
lat, lon) plus cityf, the folded town name, so that 'l'hospitalet' finds "L'Hospitalet de Llobregat". norm comes from
engine.address_search.es_norm, so the index and what people type are reduced the same way.
"""
import argparse, sqlite3, sys, time
from pathlib import Path

import duckdb

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from engine.address_search import es_norm, fold   # noqa: E402

# INE province codes (= postcode prefixes) of each autonomous community, ISO 3166-2:ES second part.
REGIONS = {'AN': ('04', '11', '14', '18', '21', '23', '29', '41'), 'AR': ('22', '44', '50'), 'AS': ('33',), 'IB': ('07',),
           'CN': ('35', '38'), 'CB': ('39',), 'CL': ('05', '09', '24', '34', '37', '40', '42', '47', '49'),
           'CM': ('02', '13', '16', '19', '45'), 'CT': ('08', '17', '25', '43'), 'VC': ('03', '12', '46'), 'EX': ('06', '10'),
           'GA': ('15', '27', '32', '36'), 'MD': ('28',), 'MC': ('30',), 'NC': ('31',), 'PV': ('01', '20', '48'),
           'RI': ('26',), 'CE': ('51',), 'ML': ('52',)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('region', choices=sorted(REGIONS))
    ap.add_argument('out', help='map set folder; the index goes to OUT/derived/geo/addresses/')
    ap.add_argument('--release', default='2026-11-18.0')
    ap.add_argument('--parquet', help='a local Parquet file with the Overture columns, instead of S3')
    a = ap.parse_args()
    code = f'ES-{a.region}'
    prefixes = ', '.join(f"'{p}'" for p in REGIONS[a.region])
    t0 = time.time()
    c = duckdb.connect()
    if a.parquet:
        src = f"read_parquet('{a.parquet}')"
        cols = 'number, street, postcode, muni, unit, lat, lon'
    else:
        c.execute("LOAD httpfs; SET s3_region='us-west-2';")
        src = (f"read_parquet('s3://overturemaps-us-west-2/release/{a.release}/theme=addresses/type=address/*', hive_partitioning=1) "
               "WHERE country = 'ES'")
        cols = 'number, street, postcode, address_levels[1].value AS muni, unit, bbox.ymin AS lat, bbox.xmin AS lon'
    where = f"{'AND' if 'WHERE' in src else 'WHERE'} left(postcode, 2) IN ({prefixes}) AND number IS NOT NULL AND street IS NOT NULL"
    rows = c.execute(f'SELECT {cols} FROM {src} {where}').fetchall()
    out = Path(a.out) / 'derived/geo/addresses'
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / f'{code}.tmp'
    tmp.unlink(missing_ok=True)
    db = sqlite3.connect(tmp)
    db.executescript('PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;'
                     'CREATE TABLE addr(num TEXT, street TEXT, norm TEXT, city TEXT, state TEXT, zip TEXT, lat REAL, lon REAL, cityf TEXT);'
                     'CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);')
    seen, n = set(), 0
    batch = []
    for number, street, postcode, muni, unit, lat, lon in rows:
        number = number.strip().upper()
        if number in ('S-N', 'SN', 'S/N'):       # "sin número": no number to find it by
            continue
        num = number + (unit.strip().upper() if unit and len(unit.strip()) == 1 and unit.strip().isalpha() else '')
        street = ' '.join(street.replace(',', ' ').split())
        key = (num, street, postcode, round(lat, 5), round(lon, 5))
        if key in seen:
            continue
        seen.add(key)
        batch.append((num, street, es_norm(street), muni or '', code, postcode or '', round(lat, 6), round(lon, 6), fold(muni or '')))
        n += 1
    db.executemany('INSERT INTO addr VALUES (?,?,?,?,?,?,?,?,?)', batch)
    db.execute('CREATE INDEX addr_norm ON addr(norm, num)')
    db.executemany('INSERT INTO meta VALUES (?,?)', [
        ('format', 'es'), ('region', code), ('count', str(n)), ('built', time.strftime('%Y-%m-%dT%H:%M:%S')),
        ('source', f'Overture Maps addresses {a.release}: scne.es, available under CC BY 4.0, distributed by OpenAddresses')])
    db.commit()
    db.execute('VACUUM')
    db.close()
    tmp.replace(out / f'{code}.sqlite')
    print(f'{code}: {n:,} addresses in {time.time() - t0:.0f}s', flush=True)


if __name__ == '__main__':
    main()
