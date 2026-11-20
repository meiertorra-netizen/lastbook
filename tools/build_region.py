"""Build one region's detailed map from tools/regions/europe.json, start to finish: the same steps locally and on GitHub Actions.

  python tools/build_region.py ES-catalunya --work /tmp/work --out dist/ [--release 2026-11-18.0] [--base-url URL]

1. Download the region's OpenStreetMap extract and boundary (the catalogue's pbf and poly) and the Copernicus GLO-30
   elevation tiles under its box (public bucket copernicus-dem-30m.s3.amazonaws.com; tiles over open sea do not exist).
2. tools/build_osm_region.py: streets, water, land, places, help points, the coast, buildings, the place finder.
3. tools/build_terrain.py: elevation tiles.
4. Spain only for now: tools/build_addresses_es.py (Overture's addresses, cut by the community's postcode prefixes). Other
   countries get no address index until their address formats are handled in engine/address_search.py.
5. tools/finish_region_map.py: credits, map-set.json and the two zips, written to --out with <id>.json, the region's
   entries for the manifest's "maps" and "address_indexes" (URLs under --base-url).
"""
import argparse, hashlib, json, math, os, shutil, subprocess, sys, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEM = 'https://copernicus-dem-30m.s3.amazonaws.com/{n}/{n}.tif'
ES_COMMUNITY = {   # catalogue slug -> the code tools/build_addresses_es.py cuts by (ISO 3166-2:ES)
    'andalucia': 'AN', 'aragon': 'AR', 'asturias': 'AS', 'illes_balears': 'IB', 'canarias': 'CN', 'cantabria': 'CB',
    'castilla_y_leon': 'CL', 'castilla_la_mancha': 'CM', 'catalunya': 'CT', 'comunitat_valenciana': 'VC', 'extremadura': 'EX',
    'galicia': 'GA', 'comunidad_de_madrid': 'MD', 'region_de_murcia': 'MC', 'comunidad_foral_de_navarra': 'NC', 'euskadi': 'PV',
    'la_rioja': 'RI', 'ceuta': 'CE', 'melilla': 'ML'}
DEM_CREDIT = ('Copernicus DEM GLO-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH 2014-2018, '
              'provided under COPERNICUS by the European Union and ESA')


def get(url, dest, required=True):
    dest = Path(dest)
    if dest.exists() and dest.stat().st_size:
        return True
    tmp = dest.with_suffix(dest.suffix + '.part')
    req = urllib.request.Request(url, headers={'User-Agent': 'lastbook-region-build'})
    try:
        with urllib.request.urlopen(req, timeout=300) as r, open(tmp, 'wb') as f:
            shutil.copyfileobj(r, f, 1 << 20)
    except urllib.error.HTTPError as e:
        tmp.unlink(missing_ok=True)
        if e.code in (403, 404) and not required:
            return False
        raise
    tmp.replace(dest)
    return True


def dem_tiles(bbox):
    w, s, e, n = bbox
    for lat in range(math.floor(s), math.ceil(n)):
        for lon in range(math.floor(w), math.ceil(e)):
            ns, ew = ('N' if lat >= 0 else 'S'), ('E' if lon >= 0 else 'W')
            yield f'Copernicus_DSM_COG_10_{ns}{abs(lat):02d}_00_{ew}{abs(lon):03d}_00_DEM'


def run(*argv):
    print('+', ' '.join(map(str, argv)), flush=True)
    subprocess.run([sys.executable, '-I', *map(str, argv)], check=True)


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('region')
    ap.add_argument('--catalog', default=str(HERE / 'regions' / 'europe.json'))
    ap.add_argument('--work', default=str(ROOT / 'derived' / 'regions'))
    ap.add_argument('--out', default=str(ROOT / 'derived' / 'regions' / 'dist'))
    ap.add_argument('--release', default='2026-11-18.0', help='Overture release for addresses')
    ap.add_argument('--base-url', default='', help='where the zips will be published, for the manifest entries')
    ap.add_argument('--keep', action='store_true', help='keep the sources and the unzipped map set')
    a = ap.parse_args()
    reg = next((r for r in json.loads(Path(a.catalog).read_text(encoding='utf-8'))['regions'] if r['id'] == a.region), None)
    if not reg:
        sys.exit(f'{a.region} is not in {a.catalog}')
    work = Path(a.work) / reg['id']
    src, mapset, out = work / 'src', work / 'map', Path(a.out)
    for d in (src / 'dem', mapset, out):
        d.mkdir(parents=True, exist_ok=True)

    get(reg['pbf'], src / 'region.osm.pbf')
    get(reg['poly'], src / 'region.poly')
    have = [n for n in dem_tiles(reg['bbox']) if get(DEM.format(n=n), src / 'dem' / f'{n}.tif', required=False)]
    print(f'{len(have)} elevation tiles', flush=True)

    osm_credit = f'OpenStreetMap contributors, via {reg["pbf"].split("//", 1)[1].split("/", 1)[0]} (ODbL 1.0)'
    run(HERE / 'build_osm_region.py', src / 'region.osm.pbf', mapset, '--name', reg['name'], '--credit', osm_credit)
    if have:
        w, s, e, n = reg['bbox']
        run(HERE / 'build_terrain.py', '--src', src / 'dem', '--out', mapset / 'derived/geo/terrain.sqlite',
            f'--bbox={w},{s},{e},{n}', '--workers', str(os.cpu_count() or 2), '--source', DEM_CREDIT)
    credit = '© OpenStreetMap contributors' + (' · Copernicus DEM © DLR e.V., © Airbus, provided under COPERNICUS by the EU and ESA' if have else '')
    if reg['country'] == 'ES' and reg['id'].split('-', 1)[1] in ES_COMMUNITY:
        run(HERE / 'build_addresses_es.py', ES_COMMUNITY[reg['id'].split('-', 1)[1]], mapset, '--release', a.release, '--code', reg['id'])
        credit += ' · Addresses: scne.es (CC BY 4.0) via Overture Maps'
    run(HERE / 'finish_region_map.py', mapset, reg['id'], '--title', reg['name'], '--credit', credit, '--zip', out)

    base = a.base_url.rstrip('/')
    entries = {}
    for kind, name in (('maps', f'Lastbook-Map-{reg["id"]}.zip'), ('address_indexes', f'Lastbook-Addresses-{reg["id"]}.zip')):
        z = out / name
        if not z.exists():
            continue
        import zipfile
        with zipfile.ZipFile(z) as zf:
            installed = sum(i.file_size for i in zf.infolist())
        entry = {'id': reg['id'], 'title': reg['name'], 'country': reg['country'], 'country_name': reg['country_name'],
                 'installed_bytes': installed, 'available': True,
                 'files': [{'name': name, 'url': f'{base}/{name}', 'size': z.stat().st_size, 'sha256': sha256(z), 'kind': 'zip'}]}
        if kind == 'maps':
            entry.update({'kind': 'region', 'includes': 'Streets, buildings, terrain, places and help points' +
                          (', and street addresses' if (out / f'Lastbook-Addresses-{reg["id"]}.zip').exists() else '')})
        entries[kind] = entry
    (out / f'{reg["id"]}.json').write_text(json.dumps(entries, indent=1, ensure_ascii=False), encoding='utf-8')
    print(json.dumps(entries, indent=1, ensure_ascii=False))
    if not a.keep:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == '__main__':
    main()
