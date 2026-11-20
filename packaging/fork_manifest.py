#!/usr/bin/env python3
"""The manifest a fork's build carries and publishes: Lastbook's own library packs, our models, and the fork's region maps.

  python3 -I -B packaging/fork_manifest.py --regions DIR [--upstream URL] --out setup/manifest-v1.json

- packs: from Lastbook's live manifest (downloads.lastbook.org), as they are: the fork serves no library of its own, so
  its distribution.json keeps downloads.lastbook.org among its library_hosts.
- models: one entry per llm_local.MODELS, as packaging/release_manifest.py writes them.
- maps and address_indexes: Lastbook's US state maps, then every region <id>.json in --regions (tools/build_region.py
  writes one per region, with the URLs of its zips on the fork's GitHub release).
The result is checked with engine/setup/manifest.validate, as setup checks it, so a build cannot ship one setup would refuse.
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'packaging')]
from engine.setup import manifest   # noqa: E402
import release_manifest             # noqa: E402

UPSTREAM = 'https://downloads.lastbook.org/manifest-v1.json'


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--regions', help='folder of <region id>.json entries from tools/build_region.py')
    ap.add_argument('--upstream', default=UPSTREAM)
    ap.add_argument('--out', required=True)
    a = ap.parse_args()
    req = urllib.request.Request(a.upstream, headers={'User-Agent': 'lastbook-fork-manifest'})
    up = json.loads(urllib.request.urlopen(req, timeout=60).read())
    maps = [dict(m, country='US', country_name='United States') for m in up.get('maps') or []]
    addresses = [dict(x, country='US', country_name='United States') for x in up.get('address_indexes') or []]
    for f in sorted(Path(a.regions).glob('*.json')) if a.regions else []:
        e = json.loads(f.read_text(encoding='utf-8'))
        if e.get('maps'):
            maps.append(e['maps'])
        if e.get('address_indexes'):
            addresses.append(e['address_indexes'])
    out = {'version': 1, 'published': up.get('published', ''), 'packs': up.get('packs') or [],
           'models': release_manifest.model_entries(), 'maps': maps, 'address_indexes': addresses}
    if manifest.validate(out) is None:
        sys.exit('setup would refuse this manifest: check the hosts in distribution.json and the entries')
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps(out, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')
    print(f'{len(out["packs"])} packs, {len(out["models"])} models, {len(maps)} maps, {len(addresses)} address indexes -> {a.out}')


if __name__ == '__main__':
    main()
