#!/usr/bin/env python3
"""Build a region's detailed map straight from an OpenStreetMap .osm.pbf extract (ODbL), for places outside the US.

  derived/buildenv/bin/python tools/build_osm_region.py catalunya.osm.pbf OUT_FOLDER --name "Catalonia" \
      --credit "OpenStreetMap contributors, via download.openstreetmap.fr catalunya.osm.pbf (ODbL 1.0)"

tools/build_map.py reads Geofabrik's "free" GeoPackage, which is not offered for every region and not for every
download host. This reads the raw extract instead (e.g. https://download.openstreetmap.fr/extracts/europe/spain/) and
writes the same tables, with the same layer and class names, so server.py and web/map.js draw it unchanged:

  OUT/derived/geo/national.sqlite   feat, feat_idx, poi, meta: roads, rail, water, land, places, help points, the sea
  OUT/derived/geo/buildings.sqlite  bldg: building outlines in the Microsoft record format (tools/build_buildings.py),
                                    with heights from height= or building:levels= for the 3D view
  OUT/derived/geo/places.sqlite     place, place_fts: the place finder (towns, villages, named help points)

The sea comes from natural=coastline: closed rings are islands, and each open chain is closed through the water on its
right-hand side (OpenStreetMap draws coastlines with the land on the left), so it works on any coast, not only a west one.
"""
import argparse, json, math, sqlite3, struct, time, zlib
from array import array
from pathlib import Path

import osmium

Q = 1e6
WORLD = 2 ** 30
REACH = 1 << 12

ROAD_Z = {'motorway': 5, 'trunk': 6, 'motorway_link': 10, 'trunk_link': 10, 'primary': 8, 'primary_link': 12,
          'secondary': 10, 'secondary_link': 13, 'tertiary': 11, 'tertiary_link': 13, 'residential': 13,
          'unclassified': 13, 'living_street': 14, 'pedestrian': 14, 'busway': 14, 'service': 15,
          'track': 13, 'track_grade1': 13, 'track_grade2': 13, 'track_grade3': 13, 'track_grade4': 13, 'track_grade5': 13,
          'footway': 15, 'path': 14, 'cycleway': 14, 'steps': 16, 'bridleway': 14}
LANDUSE_KEEP = {'park': 'park', 'forest': 'green', 'grass': 'green', 'meadow': 'green', 'scrub': 'green', 'heath': 'green',
                'recreation_ground': 'park', 'cemetery': 'park', 'allotments': 'farm', 'farmland': 'farm',
                'orchard': 'farm', 'vineyard': 'farm', 'farmyard': 'farm', 'residential': 'res', 'commercial': 'com',
                'retail': 'com', 'industrial': 'ind', 'military': 'ind', 'quarry': 'ind', 'landfill': 'ind'}
NATURAL_LAND = {'wood': 'forest', 'scrub': 'scrub', 'heath': 'heath', 'grassland': 'grass'}   # Geofabrik's names for them
PLACE_Z = {'county': 7, 'city': 6, 'town': 9, 'suburb': 12, 'village': 11, 'hamlet': 14, 'locality': 14, 'island': 11,
           'farm': 15, 'region': 5}
RAIL = {'rail', 'light_rail', 'subway', 'tram', 'narrow_gauge', 'funicular', 'monorail'}
WATERWAY = {'river', 'stream', 'canal', 'drain'}
# The help points tools/build_map.py keeps (POI_KEEP), from the tags Geofabrik derives each class from.
AMENITY = {'hospital', 'clinic', 'doctors', 'pharmacy', 'dentist', 'veterinary', 'fire_station', 'police', 'drinking_water',
           'fountain', 'shelter', 'community_centre', 'school', 'library', 'kindergarten', 'college', 'university',
           'courthouse', 'embassy', 'nursing_home', 'post_office', 'fuel'}
AMENITY_RENAME = {'toilets': 'toilet', 'townhall': 'town_hall'}
SHOP = {'supermarket': 'supermarket', 'convenience': 'convenience', 'doityourself': 'doityourself', 'hardware': 'hardware',
        'garden_centre': 'garden_centre', 'bicycle': 'bicycle_shop', 'car_repair': 'car_repair', 'chemist': 'chemist'}
MAN_MADE = {'water_tower', 'water_works', 'wastewater_plant'}


def wkb_parts(b, o=0):
    """(list of parts as flat [x, y, ...] lists, offset). Polygons yield their rings as parts."""
    e = '<' if b[o] == 1 else '>'
    t = struct.unpack_from(e + 'I', b, o + 1)[0] % 1000
    o += 5
    if t == 1:
        return [list(struct.unpack_from(e + 'dd', b, o))], o + 16
    if t == 2:
        n = struct.unpack_from(e + 'I', b, o)[0]
        return [list(struct.unpack_from(e + '%dd' % (2 * n), b, o + 4))], o + 4 + 16 * n
    if t == 3:
        nr = struct.unpack_from(e + 'I', b, o)[0]
        o += 4
        parts = []
        for _ in range(nr):
            n = struct.unpack_from(e + 'I', b, o)[0]
            parts.append(list(struct.unpack_from(e + '%dd' % (2 * n), b, o + 4)))
            o += 4 + 16 * n
        return parts, o
    if t in (4, 5, 6, 7):
        n = struct.unpack_from(e + 'I', b, o)[0]
        o += 4
        parts = []
        for _ in range(n):
            p, o = wkb_parts(b, o)
            parts.extend(p)
        return parts, o
    raise ValueError('geometry type %d' % t)


def simplify(flat, tol):
    if len(flat) <= 4 or tol <= 0:
        return flat
    out = [flat[0], flat[1]]
    px, py = flat[0], flat[1]
    for i in range(2, len(flat) - 2, 2):
        x, y = flat[i], flat[i + 1]
        if abs(x - px) > tol or abs(y - py) > tol:
            out += [x, y]
            px, py = x, y
    return out + [flat[-2], flat[-1]]


def pack(parts):
    a = array('i', [len(parts)])
    for p in parts:
        n = len(p) // 2
        a.append(n)
        lx = ly = 0
        for i in range(n):
            x, y = int(round(p[2 * i] * Q)), int(round(p[2 * i + 1] * Q))
            a.append(x - lx); a.append(y - ly)
            lx, ly = x, y
    return a.tobytes()


def bbox(parts):
    xs = [v for p in parts for v in p[0::2]]
    ys = [v for p in parts for v in p[1::2]]
    return min(xs), max(xs), min(ys), max(ys)


def area_z(minx, maxx, miny, maxy, base):
    size = max(maxx - minx, maxy - miny)
    if size <= 0:
        return 17
    return max(base, min(16, int(math.log2(360 / size)) - 4))


def poi_class(t):
    a = t.get('amenity')
    if a == 'fountain' and t.get('drinking_water') == 'yes':
        return 'drinking_water'          # Spain's public drinking fountains are tagged this way
    if a in AMENITY:
        return a
    if a in AMENITY_RENAME:
        return AMENITY_RENAME[a]
    if a == 'social_facility' and t.get('social_facility') == 'nursing_home':
        return 'nursing_home'
    if t.get('shop') in SHOP:
        return SHOP[t['shop']]
    if t.get('tourism') == 'camp_site':
        return 'camp_site'
    m = t.get('man_made')
    if m in MAN_MADE:
        return m
    if m in ('tower', 'mast') and t.get('tower:type') == 'communication':
        return 'comms_tower'
    if t.get('natural') == 'spring':
        return 'spring'
    if t.get('natural') == 'beach':
        return 'beach'
    if t.get('leisure') == 'park':
        return 'park'
    return None


def height_dm(t):
    for key, scale in (('height', 1), ('building:levels', 3)):
        v = t.get(key)
        if v:
            try:
                return int(round(float(v.split()[0].replace(',', '.')) * scale * 10))
            except ValueError:
                pass
    return 0


class Region(osmium.SimpleHandler):
    def __init__(self, out):
        super().__init__()
        self.wkb = osmium.geom.WKBFactory()
        self.db = sqlite3.connect(out / 'national.sqlite')
        self.db.executescript('''PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
          CREATE TABLE feat(id INTEGER PRIMARY KEY, layer TEXT, cls TEXT, name TEXT, minz INTEGER, geom BLOB);
          CREATE VIRTUAL TABLE feat_idx USING rtree(id, minx, maxx, miny, maxy, minz, maxz);
          CREATE TABLE poi(id INTEGER PRIMARY KEY, cls TEXT, name TEXT, lon REAL, lat REAL);
          CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);''')
        self.fid = self.pid = 0
        self.coast = []
        self.places = []            # (name, cls, lat, lon) for the place finder
        self.bldg = {}              # z14 bucket -> [array of records, count]
        self.nbldg = 0
        self.newest = 0

    def add(self, layer, cls, name, minz, parts):
        if not parts:
            return
        self.fid += 1
        self.db.execute('INSERT INTO feat VALUES (?,?,?,?,?,?)', (self.fid, layer, cls, name, minz, pack(parts)))
        self.db.execute('INSERT INTO feat_idx VALUES (?,?,?,?,?,?,?)', (self.fid, *bbox(parts), minz, 30))

    def add_poi(self, cls, name, lon, lat):
        self.pid += 1
        self.db.execute('INSERT INTO poi VALUES (?,?,?,?,?)', (self.pid, cls, name, lon, lat))

    def seen(self, o):
        try:
            self.newest = max(self.newest, o.timestamp.timestamp())
        except (AttributeError, ValueError, OSError):
            pass

    def node(self, n):
        t = n.tags
        if not t:
            return
        self.seen(n)
        name = t.get('name')
        place = t.get('place')
        if place and name:
            z = PLACE_Z.get(place, 14)
            pop = t.get('population', '')
            if place in ('city', 'town') and pop.isdigit() and int(pop) > 300000:
                z = 5
            self.add('place', place, name, z, [[n.location.lon, n.location.lat]])
            self.places.append((name, place, n.location.lat, n.location.lon))
        elif name and t.get('natural') in ('peak', 'volcano', 'saddle', 'cape'):
            ele = t.get('ele', '').split()[0] if t.get('ele') else ''
            kind = {'peak': 'mountain peak', 'volcano': 'volcano', 'saddle': 'mountain pass', 'cape': 'cape'}[t['natural']]
            self.places.append((name, kind + (f' ({ele} m)' if ele.replace('.', '', 1).isdigit() else ''), n.location.lat, n.location.lon))
        cls = poi_class(t)
        if cls:
            self.add_poi(cls, name, n.location.lon, n.location.lat)

    def way(self, w):
        t = w.tags
        if not t:
            return
        self.seen(w)
        if t.get('natural') == 'coastline':
            try:
                self.coast.append(([n.ref for n in w.nodes], [(n.lon, n.lat) for n in w.nodes]))
            except osmium.InvalidLocationError:
                pass
            return
        h, ww, r = t.get('highway'), t.get('waterway'), t.get('railway')
        if not (h or ww in WATERWAY or r in RAIL):
            return
        try:
            parts = wkb_parts(bytes.fromhex(self.wkb.create_linestring(w)))[0]
        except (RuntimeError, osmium.InvalidLocationError, ValueError):
            return
        if h:
            if h == 'track':
                g = t.get('tracktype', '')
                h = f'track_{g.replace("grade", "grade")}' if g.startswith('grade') else 'track'
            if h in ROAD_Z:
                self.add('road', h, t.get('name') or t.get('ref'), ROAD_Z[h], parts)
        elif ww in WATERWAY:
            self.add('waterway', ww, t.get('name'), 10 if ww == 'river' else 13, parts)
        else:
            self.add('rail', r, t.get('name'), 10 if r == 'rail' else 13, parts)

    def area(self, a):
        t = a.tags
        if not t:
            return
        b, nat, lu = t.get('building'), t.get('natural'), t.get('landuse')
        water = nat == 'water' or lu in ('reservoir', 'basin') or t.get('waterway') == 'riverbank' or nat == 'wetland'
        land = LANDUSE_KEEP.get(lu) or (NATURAL_LAND.get(nat) and LANDUSE_KEEP[NATURAL_LAND[nat]]) or \
            ('park' if t.get('leisure') in ('park', 'nature_reserve') else None)
        protected = t.get('boundary') in ('protected_area', 'national_park')
        cls = poi_class(t)
        if not (b and b != 'no' or water or land or protected or nat == 'beach' or cls):
            return
        try:
            parts = wkb_parts(bytes.fromhex(self.wkb.create_multipolygon(a)))[0]
        except (RuntimeError, osmium.InvalidLocationError, ValueError):
            return
        if not parts:
            return
        if b and b != 'no':
            self.building(parts[0], height_dm(t))
        elif water:
            kind = 'wetland' if nat == 'wetland' else 'reservoir' if lu == 'reservoir' or t.get('water') == 'reservoir' else \
                'riverbank' if t.get('waterway') == 'riverbank' or t.get('water') == 'river' else 'water'
            self.add('wet' if kind == 'wetland' else 'water', kind, t.get('name'), area_z(*bbox(parts), 7),
                     [simplify(p, 0.00001) for p in parts])
        elif nat == 'beach':
            self.add('beach', 'beach', t.get('name'), area_z(*bbox(parts), 10), parts)
        elif land:
            self.add('land', land, t.get('name'), area_z(*bbox(parts), 9), [simplify(p, 0.00001) for p in parts])
        elif protected:
            self.add('land', 'park', None, area_z(*bbox(parts), 8), [simplify(p, 0.00002) for p in parts])
        if cls:
            minx, maxx, miny, maxy = bbox(parts)
            self.add_poi(cls, t.get('name'), (minx + maxx) / 2, (miny + maxy) / 2)

    def building(self, ring, h):
        pts = []
        for i in range(0, len(ring) - 2, 2):        # the ring is closed; the record is not
            lon, lat = ring[i], ring[i + 1]
            s = math.sin(math.radians(max(-85, min(85, lat))))
            pts.append((int((lon + 180) / 360 * WORLD), int((0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * WORLD)))
        if len(pts) < 3:
            return
        cx = sum(p[0] for p in pts) // len(pts); cy = sum(p[1] for p in pts) // len(pts)
        rec = array('i', (len(pts), h, pts[0][0], pts[0][1]))
        lx, ly = pts[0]
        for x, y in pts[1:]:
            rec.extend((x - lx, y - ly)); lx, ly = x, y
        xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
        keys = {(i, j) for i in range((min(xs) + REACH) >> 16, ((max(xs) - REACH) >> 16) + 1)
                for j in range((min(ys) + REACH) >> 16, ((max(ys) - REACH) >> 16) + 1)} | {(cx >> 16, cy >> 16)}
        for key in keys:
            e = self.bldg.get(key)
            if e is None:
                e = self.bldg[key] = [array('i'), 0]
            e[0].extend(rec); e[1] += 1
        self.nbldg += 1


def chains(ways):
    by_start = {}
    for refs, pts in ways:
        by_start.setdefault(refs[0], []).append((refs, pts))
    used, out = set(), []
    for refs, pts in ways:
        if id(pts) in used:
            continue
        used.add(id(pts))
        r, p = list(refs), list(pts)
        while r[-1] != r[0]:
            nxt = [w for w in by_start.get(r[-1], []) if id(w[1]) not in used]
            if not nxt:
                break
            used.add(id(nxt[0][1]))
            r += nxt[0][0][1:]
            p += nxt[0][1][1:]
        out.append((r[0] == r[-1], p))
    merged = True
    while merged:
        merged = False
        opens = [c for c in out if not c[0]]
        for a in opens:
            for b in opens:
                if a is not b and a[1][-1] == b[1][0]:
                    out.remove(a); out.remove(b)
                    out.append((a[1][0] == b[1][-1], a[1] + b[1][1:]))
                    merged = True
                    break
            if merged:
                break
    return out


def clip_half(ring, axis, c, keep_low):
    """The part of a closed ring [(x, y), ...] on one side of the line x=c (axis 0) or y=c (axis 1)."""
    out = []
    n = len(ring)
    for i in range(n):
        a, b = ring[i - 1], ring[i]
        ina = (a[axis] <= c) == keep_low
        inb = (b[axis] <= c) == keep_low
        if inb:
            if not ina:
                out.append(cross(a, b, axis, c))
            out.append(b)
        elif ina:
            out.append(cross(a, b, axis, c))
    return out


def cross(a, b, axis, c):
    t = (c - a[axis]) / (b[axis] - a[axis])
    p = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)
    return (c, p[1]) if axis == 0 else (p[0], c)


def pieces(ring, box=None, most=3000):
    """Cut a huge polygon into pieces of at most ~`most` points by halving its box, so that drawing one map tile
    clips a few thousand points instead of the whole coast. The cuts add only edges along the cut lines, which
    the even-odd fill leaves invisible."""
    if box is None:
        xs = [x for x, _ in ring]; ys = [y for _, y in ring]
        box = (min(xs), min(ys), max(xs), max(ys))
    if len(ring) <= most or max(box[2] - box[0], box[3] - box[1]) < 0.02:
        return [ring] if len(ring) >= 3 else []
    axis = 0 if box[2] - box[0] >= box[3] - box[1] else 1
    c = (box[axis] + box[axis + 2]) / 2
    lo = (box[0], box[1], c, box[3]) if axis == 0 else (box[0], box[1], box[2], c)
    hi = (c, box[1], box[2], box[3]) if axis == 0 else (box[0], c, box[2], box[3])
    return pieces(clip_half(ring, axis, c, True), lo, most) + pieces(clip_half(ring, axis, c, False), hi, most)


def tangent(p, at_end, span=0.05):
    """Unit direction of the chain over its last (or first) ~5 km, pointing out of the chain."""
    a = p[-1] if at_end else p[0]
    seq = reversed(p) if at_end else p
    b = next((q for q in seq if math.hypot(q[0] - a[0], q[1] - a[1]) >= span), p[0] if at_end else p[-1])
    dx, dy = a[0] - b[0], a[1] - b[1]
    d = math.hypot(dx, dy) or 1
    return dx / d, dy / d


def sea(h, reach=6.0, ahead=0.4):
    """Islands from closed coastline rings. For each open chain, the sea on its right-hand side and the land on its
    left, each as one polygon reaching `reach` degrees from the coast. The page paints the whole view as sea and
    then a coarse world outline as land, so a region's own land polygon is what puts the coast where it really is.
    Both ends are carried `ahead` degrees on along the coast, so the sea does not stop at the extract's last point."""
    n = 0
    for closed, p in chains(h.coast):
        flat = [v for xy in p for v in xy]
        if closed:
            h.add('island', 'island', None, 0, [flat])
            n += 1
            continue
        if len(p) < 2:
            continue
        (x0, y0), (x1, y1) = p[0], p[-1]
        dx, dy = x1 - x0, y1 - y0
        d = math.hypot(dx, dy) or 1
        rx, ry = dy / d * reach, -dx / d * reach            # right of the chain's overall direction: the water
        ex, ey = tangent(p, True)
        sx, sy = tangent(p, False)
        end = (x1 + ex * ahead, y1 + ey * ahead)
        start = (x0 + sx * ahead, y0 + sy * ahead)
        ring = [start] + p + [end, (end[0] + rx, end[1] + ry), (start[0] + rx, start[1] + ry)]
        for piece in pieces(ring):
            h.add('ocean', 'ocean', None, 0, [[v for xy in piece + piece[:1] for v in xy]])
            n += 1
        ring = p + [(x1 - rx, y1 - ry), (x0 - rx, y0 - ry)]
        for piece in pieces(ring):
            h.add('island', 'mainland', None, 0, [[v for xy in piece + piece[:1] for v in xy]])
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('pbf')
    ap.add_argument('out', help='map set folder; files go to OUT/derived/geo/')
    ap.add_argument('--name', required=True, help='region name shown with OpenStreetMap places, e.g. "Catalonia"')
    ap.add_argument('--credit', required=True, help='source line stored in meta')
    a = ap.parse_args()
    geo = Path(a.out) / 'derived' / 'geo'
    geo.mkdir(parents=True, exist_ok=True)
    for f in ('national.sqlite', 'buildings.sqlite', 'places.sqlite'):
        (geo / f).unlink(missing_ok=True)
    t0 = time.time()
    h = Region(geo)
    h.apply_file(a.pbf, locations=True, idx='flex_mem')
    print(f'read: {h.fid:,} features, {h.pid:,} help points, {h.nbldg:,} buildings, {time.time() - t0:.0f}s', flush=True)
    print('sea polygons and islands:', sea(h), flush=True)
    db = h.db
    db.execute('CREATE INDEX poi_cls ON poi(cls, lon, lat)')
    minx, maxx, miny, maxy = db.execute('SELECT min(minx),max(maxx),min(miny),max(maxy) FROM feat_idx WHERE minz>0').fetchone()
    status = {'state': 'complete', 'stage': 'ready', 'features': h.fid}
    db.executemany('INSERT INTO meta VALUES (?,?)', [
        ('source', a.credit), ('data_as_of', time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(h.newest))),
        ('bounds', f'{minx:.2f},{miny:.2f},{maxx:.2f},{maxy:.2f}'), ('built', time.strftime('%Y-%m-%dT%H:%M:%S')),
        ('status', json.dumps(status))])
    db.commit()
    db.close()
    (geo / 'national-status.json').write_text(json.dumps(status), encoding='utf-8')

    b = sqlite3.connect(geo / 'buildings.sqlite')
    b.executescript('PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;'
                    'CREATE TABLE bldg(x INT, y INT, part INT, n INT, data BLOB, PRIMARY KEY(x,y,part)) WITHOUT ROWID;'
                    'CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);')
    b.executemany('INSERT INTO bldg VALUES (?,?,0,?,?)',
                  ((x, y, c, zlib.compress(buf.tobytes(), 6)) for (x, y), (buf, c) in h.bldg.items()))
    b.executemany('INSERT INTO meta VALUES (?,?)', [('buildings', str(h.nbldg)), ('built', time.strftime('%Y-%m-%dT%H:%M:%S')),
                                                   ('source', a.credit)])
    b.commit()
    b.close()

    p = sqlite3.connect(geo / 'places.sqlite')
    p.executescript('''PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
      CREATE TABLE place(id INTEGER PRIMARY KEY, name TEXT, kind TEXT, detail TEXT, lat REAL, lon REAL, rank INTEGER, source TEXT);
      CREATE VIRTUAL TABLE place_fts USING fts5(name, detail, content='place', content_rowid='id', tokenize='unicode61 remove_diacritics 2');''')
    ins = 'INSERT INTO place(name,kind,detail,lat,lon,rank,source) VALUES (?,?,?,?,?,?,?)'
    rank = {'city': 1, 'town': 1, 'village': 2, 'suburb': 2, 'county': 2, 'region': 2}
    detail = f'{a.name} (OpenStreetMap)'
    p.executemany(ins, ((name, cls, detail, lat, lon, rank.get(cls, 2 if cls.startswith(('mountain', 'volcano', 'cape')) else 3), 'OpenStreetMap') for name, cls, lat, lon in h.places))
    n = sqlite3.connect(f'file:{geo / "national.sqlite"}?mode=ro', uri=True)
    p.executemany(ins, ((name, cls.replace('_', ' '), detail, lat, lon, 4, 'OpenStreetMap')
                        for cls, name, lon, lat in n.execute("SELECT cls,name,lon,lat FROM poi WHERE name IS NOT NULL AND name<>''")))
    p.execute("INSERT INTO place_fts(place_fts) VALUES ('rebuild')")
    p.commit()
    print(f'done in {time.time() - t0:.0f}s', flush=True)


if __name__ == '__main__':
    main()
