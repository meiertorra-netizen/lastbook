"""Builds derived/geo/terrain.sqlite: elevation tiles for shaded relief, contours, spot heights and the 3D view.

  derived/buildenv/bin/python tools/build_terrain.py [--bbox W,S,E,N] [--workers N]

Source: USGS 3DEP 1 arc-second (~30 m) GeoTIFFs in library-data/geography/usgs/Elevation/1/TIFF/current.
Outside the US: --src DIR of Copernicus GLO-30 tiles (Copernicus_DSM_COG_10_N41_00_E002_00_DEM.tif, ~30 m, from the
public bucket copernicus-dem-30m.s3.amazonaws.com), with --out and --source naming the output and its credit.
Tiles are 256 px web-mercator PNGs, zoom 5-12, in the "terrain-RGB" encoding
(height_m = -10000 + (R*65536 + G*256 + B) * 0.1). Open ocean (no source data) has no tile.
"""
import argparse, math, os, re, sqlite3, struct, sys, time, zlib
from functools import lru_cache
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import warnings
warnings.filterwarnings("ignore", "Mean of empty slice")
import tifffile

HERE = Path(__file__).resolve().parent.parent
SRC = Path(os.environ.get('TERRAIN_SRC') or HERE / 'library-data/geography/usgs/Elevation/1/TIFF/current')
OUT = HERE / 'derived/geo/terrain.sqlite'
SOURCE = 'USGS 3DEP 1 arc-second DEM (library-data/geography/usgs/Elevation/1)'
COPERNICUS = re.compile(r'Copernicus_DSM_COG_\d+_([NS])(\d+)_00_([EW])(\d+)_00_DEM\.tif')
MAXZ, SUPERZ, MINZ = 12, 8, 4
SPAN = 2 ** (MAXZ - SUPERZ)          # z12 tiles per z8 supertile edge (16)
PX = 256 * SPAN                      # 4096 px


def cells():
    """(top latitude, left longitude) -> GeoTIFF path for every 1° source cell."""
    out = {}
    for d in SRC.iterdir():
        m = COPERNICUS.fullmatch(d.name)
        if m:   # named by its south-west corner
            out[(int(m[2]) * (1 if m[1] == 'N' else -1) + 1, int(m[4]) * (1 if m[3] == 'E' else -1))] = d
            continue
        m = re.fullmatch(r'([ns])(\d+)([ew])(\d+)', d.name)
        if not m:
            continue
        top = int(m[2]) * (1 if m[1] == 'n' else -1)
        left = int(m[4]) * (1 if m[3] == 'e' else -1)
        tif = next(d.glob('*.tif'), None)
        if tif:
            out[(top, left)] = tif
    return out


CELLS = None


@lru_cache(maxsize=10)
def dem(key):
    with tifffile.TiffFile(CELLS[key]) as tf:
        pg = tf.pages[0]
        a = pg.asarray().astype(np.float32)
        tie, scale = pg.tags['ModelTiepointTag'].value, pg.tags['ModelPixelScaleTag'].value
    a[a < -1000] = np.nan
    return a, tie[3], tie[4], scale[0], scale[1]


def tile_lonlat(z, x, y, n):
    """Pixel-centre longitudes (n,) and latitudes (n,) for an n-px square covering tile z/x/y."""
    t = (np.arange(n) + 0.5) / n
    lon = (x + t) / 2 ** z * 360 - 180
    merc = math.pi * (1 - 2 * (y + t) / 2 ** z)
    lat = np.degrees(np.arctan(np.sinh(merc)))
    return lon, lat


def sample(lon, lat):
    """Bilinear heights (m) on the lon x lat grid; NaN where there is no source data."""
    H = np.full((lat.size, lon.size), np.nan, np.float32)
    for top in range(math.ceil(lat.max()), math.floor(lat.min()), -1):
        for left in range(math.floor(lon.min()), math.ceil(lon.max())):
            if (top, left) not in CELLS:
                continue
            a, ox, oy, sx, sy = dem((top, left))
            rows = np.nonzero((lat <= top) & (lat > top - 1))[0]
            cols = np.nonzero((lon >= left) & (lon < left + 1))[0]
            if not rows.size or not cols.size:
                continue
            fr = (oy - lat[rows]) / sy - 0.5
            fc = (lon[cols] - ox) / sx - 0.5
            r0 = np.clip(np.floor(fr).astype(int), 0, a.shape[0] - 2); c0 = np.clip(np.floor(fc).astype(int), 0, a.shape[1] - 2)
            wr = np.clip(fr - r0, 0, 1)[:, None].astype(np.float32); wc = np.clip(fc - c0, 0, 1)[None, :].astype(np.float32)
            v = (a[np.ix_(r0, c0)] * (1 - wr) * (1 - wc) + a[np.ix_(r0 + 1, c0)] * wr * (1 - wc)
                 + a[np.ix_(r0, c0 + 1)] * (1 - wr) * wc + a[np.ix_(r0 + 1, c0 + 1)] * wr * wc)
            H[np.ix_(rows, cols)] = v
    return H


def png(h):
    """256x256 heights (m, NaN = sea level) -> terrain-RGB PNG bytes (filter 'Up' per row for compression)."""
    v = np.clip(np.round((np.nan_to_num(h, nan=0.0) + 10000) * 10), 0, 2 ** 24 - 1).astype(np.uint32)
    rgb = np.stack([(v >> 16) & 255, (v >> 8) & 255, v & 255], -1).astype(np.uint8).reshape(h.shape[0], -1)
    up = rgb.copy(); up[1:] = rgb[1:] - rgb[:-1]
    raw = np.hstack([np.full((h.shape[0], 1), 2, np.uint8), up]).tobytes()
    chunk = lambda t, d: struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', h.shape[1], h.shape[0], 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 6)) + chunk(b'IEND', b''))


def down(H):
    """Halve resolution, ignoring NaN (mean of available samples)."""
    s = H.reshape(H.shape[0] // 2, 2, H.shape[1] // 2, 2)
    with np.errstate(invalid='ignore'):
        return np.nanmean(s, axis=(1, 3)).astype(np.float32) if np.isnan(s).any() else s.mean(axis=(1, 3))


def supertile(args):
    """Build every z8..z12 tile inside one z8 tile. Returns (tiles, z8 heights)."""
    x8, y8 = args
    lon, lat = tile_lonlat(SUPERZ, x8, y8, PX)
    H = sample(lon, lat)
    if np.isnan(H).all():
        return [], None
    out = []
    for z in range(MAXZ, SUPERZ - 1, -1):
        n = 2 ** (z - SUPERZ)
        for j in range(n):
            for i in range(n):
                h = H[j * 256:(j + 1) * 256, i * 256:(i + 1) * 256]
                if not np.isnan(h).all():
                    out.append((z, x8 * n + i, y8 * n + j, png(h)))
        if z > SUPERZ:
            H = down(H)
    return out, H


def init(src=None):
    global CELLS, SRC
    if src:
        SRC = Path(src)
    CELLS = cells()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--bbox', help='W,S,E,N limit (default: everything)')
    ap.add_argument('--workers', type=int, default=24)
    ap.add_argument('--src', help='folder of source GeoTIFFs (default: the USGS 3DEP folder)')
    ap.add_argument('--out', help='output database (default: derived/geo/terrain.sqlite)')
    ap.add_argument('--source', help='credit stored in meta (default: USGS 3DEP)')
    a = ap.parse_args()
    global OUT
    if a.src:
        os.environ['TERRAIN_SRC'] = str(Path(a.src).resolve())   # the workers read it at import
    if a.out:
        OUT = Path(a.out)
    init(a.src)
    keys = set()
    for top, left in CELLS:
        if a.bbox:
            W, S, E, N = map(float, a.bbox.split(','))
            if left + 1 < W or left > E or top < S or top - 1 > N:
                continue
        lat0, lat1 = max(-84, top - 1), min(84, top)
        n = 2 ** SUPERZ
        x0, x1 = int((left + 180) / 360 * n), int((left + 1 + 180 - 1e-9) / 360 * n)
        ys = [int((1 - math.log(math.tan(math.radians(l)) + 1 / math.cos(math.radians(l))) / math.pi) / 2 * n) for l in (lat1 - 1e-9, lat0)]
        for x in range(x0, x1 + 1):
            for y in range(ys[0], ys[1] + 1):
                keys.add((x, y))
    keys = sorted(keys, key=lambda k: (k[1] // 2, k[0], k[1]))  # neighbours together so source cells stay cached
    db = sqlite3.connect(OUT)
    db.executescript('PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; CREATE TABLE IF NOT EXISTS tile(z INT, x INT, y INT, png BLOB, PRIMARY KEY(z,x,y)) WITHOUT ROWID;'
                     'CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);'
                     'CREATE TABLE IF NOT EXISTS done(x INT, y INT, h8 BLOB, PRIMARY KEY(x,y)) WITHOUT ROWID;')
    finished = {(x, y) for x, y in db.execute('SELECT x,y FROM done')}
    low = {(x, y): np.frombuffer(b, np.float32).reshape(256, 256) for x, y, b in db.execute('SELECT x,y,h8 FROM done WHERE h8 IS NOT NULL')}
    todo = [k for k in keys if k not in finished]
    print(f'{len(finished)} supertiles already built, {len(todo)} to go', flush=True)
    t0, done, ntiles = time.time(), 0, 0
    with ProcessPoolExecutor(a.workers, initializer=init) as pool:
        futs = [pool.submit(_job, k) for k in todo]
        for f in as_completed(futs):
            tiles, h8, key = f.result()  # a dead worker raises here instead of hanging
            db.executemany('INSERT OR REPLACE INTO tile VALUES (?,?,?,?)', tiles)
            db.execute('INSERT OR REPLACE INTO done VALUES (?,?,?)', (*key, None if h8 is None else h8.astype(np.float32).tobytes()))
            ntiles += len(tiles); done += 1
            if h8 is not None:
                low[key] = h8
            if done % 50 == 0:
                db.commit()
                print(f'{done + len(finished)}/{len(keys)} supertiles, {ntiles:,} tiles, {time.time() - t0:.0f}s', flush=True)
    db.commit()
    # zoom 7..4 from the z8 heights
    level = low
    for z in range(SUPERZ - 1, MINZ - 1, -1):
        nxt = {}
        for (x, y) in {(x // 2, y // 2) for x, y in level}:
            q = np.full((512, 512), np.nan, np.float32)
            for dy in (0, 1):
                for dx in (0, 1):
                    h = level.get((2 * x + dx, 2 * y + dy))
                    if h is not None:
                        q[dy * 256:(dy + 1) * 256, dx * 256:(dx + 1) * 256] = h
            h = down(q)
            nxt[(x, y)] = h
            db.execute('INSERT OR REPLACE INTO tile VALUES (?,?,?,?)', (z, x, y, png(h)))
        level = nxt
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('built', time.strftime('%Y-%m-%dT%H:%M:%S')))
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('source', a.source or SOURCE))
    db.commit()
    print(f'done: {done} supertiles, {ntiles:,} tiles in {time.time() - t0:.0f}s', flush=True)


def _job(key):
    tiles, h = supertile(key)
    return tiles, h, key


if __name__ == '__main__':
    main()
