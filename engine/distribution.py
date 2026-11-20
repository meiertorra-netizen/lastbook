"""Who publishes this build, and where its downloads come from.

Lastbook's own builds use the defaults below: downloads.lastbook.org for packs, maps, the manifest and the update check, and
Hugging Face for the AI models. A build of a fork can name itself and its own download site instead, without changing any
other file: packaging writes distribution.json beside the engine (in the app: next to the program's files; in a checkout:
the repository's top folder), and this module reads it once, at import.

  {"app_name": "Lastbook Europa", "bundle_id": "org.example.lastbook-europa", "website": "github.com/you/lastbook",
   "manifest_url": "https://github.com/you/lastbook/releases/download/maps/manifest-v1.json",
   "update_url": "https://github.com/you/lastbook/releases/download/app/app-v1.json",
   "library_hosts": ["github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"],
   "extra_roots": ["USERTrust ECC Certification Authority", ...]}

Every host named here is added to the downloader's allow-list (engine/setup/download.py), so it must also verify through
engine/setup/roots.pem: packaging/check_tls_roots.py checks them all before a build ships, and packaging/setup_roots.py
adds extra_roots (by their name in Mozilla's list) to that file for such a build.
"""
import json
from pathlib import Path
from urllib.parse import urlsplit

DEFAULTS = {
    'app_name': 'Lastbook',
    'bundle_id': 'local.lastbook.desktop',
    'website': 'lastbook.org',
    'manifest_url': 'https://downloads.lastbook.org/manifest-v1.json',
    'update_url': 'https://downloads.lastbook.org/app-v1.json',
    'library_hosts': ['downloads.lastbook.org'],
    'model_hosts': ['huggingface.co'],
    'extra_roots': [],
}


def _load():
    here = Path(__file__).resolve().parent
    for p in (here.parent / 'distribution.json', here / 'distribution.json'):
        try:
            d = json.loads(p.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(d, dict):
            return {k: d.get(k, v) if isinstance(d.get(k, v), type(v)) else v for k, v in DEFAULTS.items()}
    return dict(DEFAULTS)


D = _load()
APP_NAME = D['app_name']
BUNDLE_ID = D['bundle_id']
WEBSITE = D['website']
MANIFEST_URL = D['manifest_url']
UPDATE_URL = D['update_url']
LIBRARY_HOSTS = frozenset(D['library_hosts'] + [urlsplit(D['manifest_url']).hostname])
MODEL_HOSTS = frozenset(D['model_hosts'])
UPDATE_HOST = urlsplit(D['update_url']).hostname
EXTRA_ROOTS = list(D['extra_roots'])
OFFICIAL = D == DEFAULTS
