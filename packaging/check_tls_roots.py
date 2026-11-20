#!/usr/bin/env python3
"""Release gate: setup's downloader must verify every host it downloads from, through its own roots only.

  python3 -I -B packaging/check_tls_roots.py        exit 0 = every host verifies and the outside site is refused; 1 = do not ship

It connects (TLS handshake and one HEAD, nothing downloaded) to each allowed host through engine/setup/download.tls_context(),
which trusts only engine/setup/roots.pem. A host that fails means its chain moved to a root outside that set: refresh
roots.pem (packaging/setup_roots.py) before shipping. It also checks that a site whose chain ends in a root outside the set is
refused, which shows the system's trust store is not being used.
"""
import http.client
import ssl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine.setup import download  # noqa: E402

HOSTS = sorted(download.ALLOWED_HOSTS) + ['us.aws.cdn.hf.co', 'us.gcp.cdn.hf.co', 'cdn-lfs.hf.co', 'cdn-lfs-us-1.hf.co']   # *.hf.co seen on 12 Nov 2026
# A site whose chain ends in a root that roots.pem does not hold: it must be refused. Lastbook's own roots hold no DigiCert
# root; a fork's (engine/distribution.py extra_roots) may, for GitHub's downloads, so the first site of a CA family that
# roots.pem's labels do not name is used.
OUTSIDE_CANDIDATES = [('www.digicert.com', 'DigiCert'), ('www.ssl.com', 'SSL.com'), ('www.certum.pl', 'Certum'), ('www.buypass.com', 'Buypass')]
_LABELS = [l[1:].strip() for l in (Path(download.__file__).parent / 'roots.pem').read_text(encoding='utf-8').splitlines() if l.startswith('#')]
OUTSIDE = next(h for h, family in OUTSIDE_CANDIDATES if not any(family in label for label in _LABELS))


def handshake(host, ctx):
    c = http.client.HTTPSConnection(host, 443, timeout=20, context=ctx)
    try:
        c.request('HEAD', '/', headers={'User-Agent': download.USER_AGENT})
        return c.getresponse().status
    finally:
        c.close()


def main():
    ctx = download.tls_context()
    print(f'roots: {download.roots_path()} ({ctx.cert_store_stats()["x509_ca"]} trusted)')
    bad = []
    for h in HOSTS:
        try:
            print(f'  ok   {h} (HTTP {handshake(h, ctx)})')
        except ssl.SSLCertVerificationError as e:
            bad.append(h)
            print(f'  FAIL {h}: {e.verify_message}')
        except OSError as e:
            bad.append(h)
            print(f'  FAIL {h}: {type(e).__name__}: {e}')
    try:
        handshake(OUTSIDE, ctx)
        bad.append(OUTSIDE + ' (accepted, but its root is not in roots.pem: the system store is in use)')
        print(f'  FAIL {OUTSIDE} was accepted')
    except ssl.SSLCertVerificationError:
        print(f'  ok   {OUTSIDE} refused, as it should be')
    except OSError as e:
        bad.append(f'{OUTSIDE} (could not reach it: {type(e).__name__})')
        print(f'  FAIL {OUTSIDE}: {type(e).__name__}: {e}')
    print('TLS ROOTS FAILED: ' + ', '.join(bad) if bad else 'TLS ROOTS PASSED')
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
