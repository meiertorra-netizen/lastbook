"""The catalogue of European regions Lastbook can build a detailed map for: tools/regions/europe.json.

  python3 tools/region_catalog.py [--source osmfr|geofabrik] [--out tools/regions/europe.json]

One entry per region: a country's first-level subdivision where an extract of it is published, else the whole country.
Each names the OpenStreetMap extract to build from (.osm.pbf) and its boundary (.poly), and gives the region's box, so
tools/build_region.py needs nothing else to find its sources:

  {"id": "ES-catalunya", "country": "ES", "country_name": "Spain", "name": "Catalunya", "pbf": "https://...", "poly": "https://...",
   "bbox": [W, S, E, N], "area_km2": 32108}

Ids are the country's ISO 3166-1 code and a plain slug of the region's name, whichever source listed it, so a map keeps
its id if its source changes. Sources: OSM France's extracts (download.openstreetmap.fr, region by region for most
countries) and Geofabrik's (download.geofabrik.de, whose index lists every extract with its ISO code; used where it is
reachable, e.g. on GitHub Actions). Both publish OpenStreetMap data under the ODbL.
"""
import argparse, json, math, re, sys, unicodedata, urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
OSMFR = 'https://download.openstreetmap.fr/extracts/europe/'
OSMFR_POLY = 'https://download.openstreetmap.fr/polygons/europe/'
GEOFABRIK_INDEX = 'https://download.geofabrik.de/index-v1-nogeom.json'

COUNTRIES = {   # OSM France folder -> (ISO 3166-1, English name)
    'albania': ('AL', 'Albania'), 'andorra': ('AD', 'Andorra'), 'austria': ('AT', 'Austria'), 'belarus': ('BY', 'Belarus'),
    'belgium': ('BE', 'Belgium'), 'bosnia-herzegovina': ('BA', 'Bosnia and Herzegovina'), 'bulgaria': ('BG', 'Bulgaria'),
    'croatia': ('HR', 'Croatia'), 'cyprus': ('CY', 'Cyprus'), 'czech_republic': ('CZ', 'Czech Republic'),
    'denmark': ('DK', 'Denmark'), 'estonia': ('EE', 'Estonia'), 'faroe_islands': ('FO', 'Faroe Islands'),
    'finland': ('FI', 'Finland'), 'france': ('FR', 'France'), 'germany': ('DE', 'Germany'), 'gibraltar': ('GI', 'Gibraltar'),
    'greece': ('GR', 'Greece'), 'guernesey': ('GG', 'Guernsey'), 'hungary': ('HU', 'Hungary'), 'iceland': ('IS', 'Iceland'),
    'ireland': ('IE', 'Ireland'), 'isle_of_man': ('IM', 'Isle of Man'), 'italy': ('IT', 'Italy'), 'jersey': ('JE', 'Jersey'),
    'kosovo': ('XK', 'Kosovo'), 'latvia': ('LV', 'Latvia'), 'liechtenstein': ('LI', 'Liechtenstein'),
    'lithuania': ('LT', 'Lithuania'), 'luxembourg': ('LU', 'Luxembourg'), 'malta': ('MT', 'Malta'), 'moldova': ('MD', 'Moldova'),
    'monaco': ('MC', 'Monaco'), 'montenegro': ('ME', 'Montenegro'), 'netherlands': ('NL', 'Netherlands'),
    'north-macedonia': ('MK', 'North Macedonia'), 'macedonia': ('MK', 'North Macedonia'), 'norway': ('NO', 'Norway'),
    'poland': ('PL', 'Poland'), 'portugal': ('PT', 'Portugal'), 'romania': ('RO', 'Romania'), 'san_marino': ('SM', 'San Marino'),
    'serbia': ('RS', 'Serbia'), 'slovakia': ('SK', 'Slovakia'), 'slovenia': ('SI', 'Slovenia'), 'spain': ('ES', 'Spain'),
    'sweden': ('SE', 'Sweden'), 'switzerland': ('CH', 'Switzerland'), 'turkey': ('TR', 'Turkey'), 'ukraine': ('UA', 'Ukraine'),
    'united_kingdom': ('GB', 'United Kingdom'), 'vatican_city': ('VA', 'Vatican City'),
}
SMALL = ('de', 'del', 'la', 'las', 'los', 'y', 'i', 'el', 'and', 'of', 'the', 'du', 'des', 'et', 'le', 'di', 'e')
NAMES = {   # region names a slug cannot spell
    'ES-catalunya': 'Catalunya / Catalonia', 'ES-comunitat_valenciana': 'Comunitat Valenciana', 'ES-euskadi': 'Euskadi / País Vasco',
    'ES-andalucia': 'Andalucía', 'ES-aragon': 'Aragón', 'ES-castilla_y_leon': 'Castilla y León', 'ES-region_de_murcia': 'Región de Murcia',
    'ES-illes_balears': 'Illes Balears', 'ES-canarias': 'Canarias / Canary Islands', 'ES-castilla_la_mancha': 'Castilla-La Mancha', 'ES-comunidad_de_madrid': 'Comunidad de Madrid', 'ES-comunidad_foral_de_navarra': 'Navarra',
}


def slug(text):
    t = unicodedata.normalize('NFKD', text).encode('ascii', 'ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+', '_', t).strip('_')


def title(s):
    words = s.replace('-', ' ').replace('_', ' ').split()
    return ' '.join(w if i and w in SMALL else w.capitalize() for i, w in enumerate(words))


def fetch(url, timeout=60):
    req = urllib.request.Request(url, headers={'User-Agent': 'lastbook-region-catalog'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def poly_box(text):
    """[W, S, E, N] and an approximate area (km2) of an Osmosis .poly file (outer rings, holes subtracted)."""
    xs, ys, area, ring, hole = [], [], 0.0, [], False
    lines = [l.strip() for l in text.splitlines()]
    for l in lines[1:]:
        if l == 'END':
            if ring:
                a = 0.0
                for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
                    a += math.radians(x1 - x0) * (2 + math.sin(math.radians(y0)) + math.sin(math.radians(y1)))
                area += (-1 if hole else 1) * abs(a) * 6371.0 ** 2 / 2
                ring = []
            continue
        if not l:
            continue
        if not re.match(r'^-?[\d.eE+-]+\s+-?[\d.eE+-]+$', l):
            hole = l.startswith('!')
            continue
        x, y = map(float, l.split())
        ring.append((x, y)); xs.append(x); ys.append(y)
    return [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)], int(area)


def listing(url):
    html = fetch(url).decode('utf-8', 'replace')
    return re.findall(r'<a href="([^"?/][^"?]*)">', html)


EXTRA_DIRS = {'africa/spain': ('ES', 'Spain')}     # the Canary Islands, Ceuta and Melilla are filed under Africa


def from_osmfr():
    out = []
    top = listing(OSMFR)
    dirs = [e.rstrip('/') for e in top if e.endswith('/') and e.rstrip('/') in COUNTRIES]
    single = [e[:-len('.osm.pbf')] for e in top if e.endswith('.osm.pbf') and not e.endswith('-latest.osm.pbf')]
    jobs = [('europe/' + d, *COUNTRIES[d], True) for d in dirs]
    jobs += [('europe/' + c, *COUNTRIES[c], False) for c in single if c in COUNTRIES and c not in dirs]
    jobs += [(d, cc, name, True) for d, (cc, name) in EXTRA_DIRS.items()]
    base = OSMFR.rsplit('europe/', 1)[0]
    pbase = OSMFR_POLY.rsplit('europe/', 1)[0]
    for path, cc, cname, has_dir in jobs:
        kids = []
        if has_dir:
            kids = [k[:-len('.osm.pbf')] for k in listing(base + path + '/') if k.endswith('.osm.pbf') and not k.endswith('-latest.osm.pbf')]
        if kids:
            targets = [(f'{cc}-{slug(k)}', title(k), f'{base}{path}/{k}.osm.pbf', f'{pbase}{path}/{k}.poly') for k in kids]
        else:
            targets = [(cc, cname, f'{base}{path}.osm.pbf', f'{pbase}{path}.poly')]
        for rid, name, pbf, poly in targets:
            try:
                bbox, area = poly_box(fetch(poly).decode('utf-8', 'replace'))
            except Exception as e:
                print(f'skip {rid}: no boundary ({type(e).__name__})', file=sys.stderr)
                continue
            out.append({'id': rid, 'country': cc, 'country_name': cname, 'name': NAMES.get(rid, name),
                        'pbf': pbf, 'poly': poly, 'bbox': bbox, 'area_km2': area, 'source': 'osmfr'})
            print(rid, out[-1]['name'], bbox, area, file=sys.stderr)
    return out


def from_geofabrik():
    idx = json.loads(fetch(GEOFABRIK_INDEX))
    feats = [f['properties'] for f in idx['features']]
    by_id = {p['id']: p for p in feats}
    europe = [p for p in feats if p.get('parent') == 'europe']
    out = []
    for c in europe:
        cc = (c.get('iso3166-1:alpha2') or [''])[0]
        if not cc:
            continue
        kids = [p for p in feats if p.get('parent') == c['id']]
        for p in kids or [c]:
            iso2 = (p.get('iso3166-2') or [''])[0] if kids else ''
            rid = (f'{cc}-{slug(p["name"])}' if kids else cc)
            urls = p.get('urls') or {}
            out.append({'id': rid, 'iso3166_2': iso2 or None, 'country': cc, 'country_name': c['name'], 'name': NAMES.get(rid, p['name']),
                        'pbf': urls.get('pbf'), 'poly': urls.get('pbf', '').replace('-latest.osm.pbf', '.poly'),
                        'bbox': None, 'area_km2': None, 'source': 'geofabrik'})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', choices=('osmfr', 'geofabrik'), default='osmfr')
    ap.add_argument('--out', default=str(HERE / 'regions' / 'europe.json'))
    a = ap.parse_args()
    regions = from_geofabrik() if a.source == 'geofabrik' else from_osmfr()
    regions.sort(key=lambda r: (r['country_name'], r['name']))
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps({'source': a.source, 'regions': regions}, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'{len(regions)} regions in {len({r["country"] for r in regions})} countries -> {a.out}')


if __name__ == '__main__':
    main()
