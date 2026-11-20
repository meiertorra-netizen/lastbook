"""Check for updates, in the desktop app: only when the person asks (About's button, or Lastbook > Check for Updates… on a Mac).
Nothing here runs at startup or on a timer, and nothing is downloaded, installed or started: the check only says whether a newer
version is on the website.

  - One small GET of METADATA through engine/setup/download.py, the one way out (HTTPS, no proxy). Only https://downloads.lastbook.org
    on port 443 is accepted, also after a redirect: not the bucket's r2.dev address, not Hugging Face.
  - METADATA (app-v1.json at the bucket root) is display information only: {"version": 1, "mac": {"version", "min_os"}, "windows": {...}}.
    Other fields (url, size, sha256, notes) may be there for later; they are not read.
  - Later, with Developer ID and Authenticode signing: signed metadata (an Ed25519 key in the app) and an in-app install. Not before.

Routes, under /api/update/: GET state; POST check.
"""
import json
import os
import platform
import plistlib
import re
import sys
import threading
import traceback
import urllib.parse
from pathlib import Path

from engine.setup import download

from engine import distribution   # noqa: E402
METADATA = distribution.UPDATE_URL
HOST = distribution.UPDATE_HOST
VER = re.compile(r'^\d{1,6}(\.\d{1,6}){0,3}$')    # Windows builds have 5 digits (10.0.26200)
MAX_BODY = 4096
WEBSITE = distribution.WEBSITE

OFFLINE = 'Lastbook could not reach the internet. Everything else in Lastbook still works without it; try again when you are online.'


def _ver(s):
    return tuple(int(x) for x in s.split('.')) if isinstance(s, str) and VER.match(s) else None


def platform_key():
    return 'mac' if sys.platform == 'darwin' else 'windows' if sys.platform == 'win32' else 'linux' if sys.platform.startswith('linux') else None


def _mac_bundle():
    if not getattr(sys, 'frozen', False):
        return None
    for b in Path(sys.executable).resolve().parents:
        if b.suffix == '.app':
            return b
    return None


def _windows_exe_version(path):
    import ctypes
    from ctypes import wintypes as wt
    v = ctypes.windll.version
    n = v.GetFileVersionInfoSizeW(str(path), None)
    if not n:
        return None
    buf = ctypes.create_string_buffer(n)
    if not v.GetFileVersionInfoW(str(path), 0, n, buf):
        return None
    p, ln = ctypes.c_void_p(), wt.UINT()
    if not v.VerQueryValueW(buf, '\\', ctypes.byref(p), ctypes.byref(ln)) or ln.value < 52:
        return None
    f = ctypes.cast(p, ctypes.POINTER(ctypes.c_uint32 * 13)).contents      # VS_FIXEDFILEINFO: [2] FileVersionMS, [3] FileVersionLS
    return f'{f[2] >> 16}.{f[2] & 0xFFFF}.{f[3] >> 16}'


def app_version():
    """This app's version as 'X.Y.Z', or None outside a built app (LASTBOOK_VERSION stands in for tests)."""
    try:
        if sys.platform == 'darwin' and _mac_bundle():
            with open(_mac_bundle() / 'Contents' / 'Info.plist', 'rb') as f:
                return str(plistlib.load(f).get('CFBundleShortVersionString') or '') or None
        if sys.platform == 'win32' and getattr(sys, 'frozen', False):
            return _windows_exe_version(sys.executable)
        if sys.platform.startswith('linux') and getattr(sys, 'frozen', False):    # the .deb's /opt/lastbook/VERSION
            v = (Path(sys._MEIPASS) / 'VERSION').read_text(encoding='utf-8').strip()
            return v if VER.match(v) else None
    except Exception:
        traceback.print_exc()
    return os.environ.get('LASTBOOK_VERSION') or None


# Ubuntu releases by code name, for systems built on Ubuntu that number their own releases (Linux Mint 22 and elementary OS 8 are
# both Ubuntu 24.04 underneath) and name the Ubuntu they are built on in os-release's UBUNTU_CODENAME. A name missing here is
# simply not known: the check then says nothing about the system, as on any other Linux.
UBUNTU_CODENAMES = {'jammy': '22.04', 'noble': '24.04', 'oracular': '24.10', 'plucky': '25.04', 'questing': '25.10', 'resolute': '26.04'}


def linux_version(release):
    """The Ubuntu version this Linux is, or is built on, from os-release's fields; None when it cannot be told. Only Ubuntu's
    numbers compare with the Linux min_os: Mint's 22 or Fedora's 43 are not Ubuntu versions."""
    if release.get('ID') == 'ubuntu':
        return release.get('VERSION_ID') or None
    return UBUNTU_CODENAMES.get(release.get('UBUNTU_CODENAME', '').lower())


def os_version():
    """This system's version as min_os counts it, or None when it cannot be told (and then no system is called too old)."""
    if sys.platform == 'darwin':
        return platform.mac_ver()[0] or None
    if sys.platform == 'win32':
        w = sys.getwindowsversion()
        return f'{w.major}.{w.minor}.{w.build}'
    if sys.platform.startswith('linux'):
        try:
            return linux_version(platform.freedesktop_os_release())
        except OSError:
            return None
    return None


def validate(m, key):
    """This platform's version and minimum system from app-v1.json, or None if they are not there or not plain version numbers."""
    if not isinstance(m, dict) or m.get('version') != 1 or not isinstance(m.get(key), dict):
        return None
    e = m[key]
    if not _ver(e.get('version')):
        return None
    return {'version': e['version'], 'min_os': e['min_os'] if _ver(e.get('min_os')) else None}


OS_NAMES = {'mac': 'macOS', 'windows': 'Windows', 'linux': 'Ubuntu'}


def _has_no_entry(data, key):
    try:
        m = json.loads(data)
    except ValueError:
        return False
    return isinstance(m, dict) and m.get('version') == 1 and key not in m


class Updater:
    """allowed and context are for tests (a local HTTPS server); the product passes neither and reaches only HOST on port 443."""

    def __init__(self, url=METADATA, allowed=None, context=None, key=None, version=None):
        self.url, self.context = url, context
        self.product = allowed is None
        self.allowed = {HOST} if allowed is None else allowed
        self.key = key or platform_key()
        self.version = version or app_version()
        self.lock = threading.Lock()
        self.s = {'state': 'idle'}
        self.thread = None

    def state(self):
        with self.lock:
            out = dict(self.s)
        out['current'] = self.version
        out['website'] = WEBSITE
        return out

    def _set(self, **kw):
        with self.lock:
            self.s = kw

    def check(self):
        if not self.key:
            return {'error': 'Updates are for the Mac, Windows and Linux app.', 'code': 'unsupported'}
        if self.thread is not None and self.thread.is_alive():
            return self.state()
        self._set(state='checking')
        self.thread = threading.Thread(target=self._guard, daemon=True, name='update-check')
        self.thread.start()
        return self.state()

    def _guard(self):
        try:
            self._check()
        except Exception as e:
            traceback.print_exc()
            self._set(state='error', code='internal', message=f'The check stopped on an internal error ({type(e).__name__}).')

    def _fetch(self):
        """(status, bytes) of the metadata through download.py. Raises download.Failed with code 'offline' when nothing answers."""
        if self.product and urllib.parse.urlsplit(self.url).port not in (None, 443):
            raise download.Failed('network', 'Lastbook refused to check an address it does not know.')
        try:
            conn, r, final = download._open(self.url, 0, None, self.allowed, self.context or download.tls_context(), 15)
        except download.Failed:
            raise
        except download.ssl.SSLCertVerificationError:
            raise download.Failed('network', 'Lastbook could not confirm it was talking to the real download site, so it stopped. '
                                             'A network that intercepts secure connections (some offices, hotels) can cause this.')
        except (OSError, ValueError, download.http.client.HTTPException):   # names through download.py: no network module here
            raise download.Failed('offline', OFFLINE)
        try:
            if self.product and urllib.parse.urlsplit(final).port not in (None, 443):     # a redirect on HOST to another port
                return 0, b''
            data = r.read(MAX_BODY + 1) if r.status == 200 else b''
            return r.status, data
        except OSError:
            raise download.Failed('offline', OFFLINE)
        finally:
            conn.close()

    def _check(self):
        try:
            status, data = self._fetch()
        except download.Failed as f:
            return self._set(state='error', code=f.code, message=f.message)
        if status in (403, 404):
            return self._set(state='not_published', message='No update information yet. Try again later.')
        if status != 200 or len(data) > MAX_BODY:
            return self._set(state='error', code='server', message='The download site did not answer properly. Try again later.')
        try:
            offer = validate(json.loads(data), self.key)
        except ValueError:
            offer = None
        if not offer and self.key == 'linux' and _has_no_entry(data, self.key):   # Linux is announced only once the site lists it
            return self._set(state='not_published', message='No update information yet. Try again later.')
        if not offer:
            return self._set(state='error', code='bad_info', message='The update information on the download site could not be read. Try again later.')
        mine = _ver(self.version or '')
        if mine and _ver(offer['version']) <= mine:
            return self._set(state='up_to_date', latest=offer['version'])
        have = _ver(os_version())
        if offer['min_os'] and have and have < _ver(offer['min_os']):
            built_on = ', or a system built on it' if self.key == 'linux' else ''
            return self._set(state='too_old_os', version=offer['version'],
                             message=f"Version {offer['version']} needs {OS_NAMES.get(self.key, self.key)} {offer['min_os']} or later{built_on}.")
        self._set(state='available', version=offer['version'])


UPDATER = None


def updater():
    global UPDATER
    if UPDATER is None:
        UPDATER = Updater()
    return UPDATER


ROUTES = {
    ('GET', 'state'): lambda u: u.state(),
    ('POST', 'check'): lambda u: u.check(),
}


def handle(h, method, path):
    """Answers one /api/update/ request on a BaseHTTPRequestHandler h. This computer only."""
    from engine.setup.api import _send_json
    if h.client_address[0] not in ('127.0.0.1', '::1', '::ffff:127.0.0.1'):
        return _send_json(h, {'error': 'Updates are only open on this computer.'}, 403)
    fn = ROUTES.get(('GET' if method == 'HEAD' else method, path[len('/api/update/'):].strip('/')))
    if not fn:
        return _send_json(h, {'error': 'not found'}, 404)
    if method == 'POST':
        try:
            n = int(h.headers.get('Content-Length') or 0)
            if 0 < n <= 4096:
                h.rfile.read(n)
        except ValueError:
            pass
    try:
        out = fn(updater())
    except Exception as e:
        traceback.print_exc()
        return _send_json(h, {'error': f'The check stopped on an internal error ({type(e).__name__}).'}, 500)
    return _send_json(h, out)
