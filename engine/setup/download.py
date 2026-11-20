"""The one way out: downloads for setup. This is the only module in Lastbook that opens a network connection.

  - HTTPS only, to the hosts in ALLOWED_HOSTS (or a subdomain of ALLOWED_SUFFIXES), port 443.
  - No proxy: http.client never reads the system's or the environment's proxy settings (urllib would), so a request goes
    straight to the host named, or nowhere.
  - A redirect is followed only to another allowed address, at most MAX_REDIRECTS times.
  - Resuming uses HTTP Range on the ETag saved beside the partial file. R2 ignores If-Range (it answers 206 whatever the ETag),
    so the check is made here: another ETag on the 206, a 200 instead, or another total size starts that file over.
    (Not If-Match: on a redirect chain the first host's ETag is not the file's, so it would refuse every resume.)
  - The SHA-256 is computed while the bytes arrive. After a resume the bytes already on disk are read once to rebuild it.
  - A file of PARALLEL_MIN or more comes over CONNECTIONS connections at once, each asking for one PIECE with a closed Range
    (bytes=a-b). Pieces are handed out in order and at most AHEAD pieces past the first unfinished one, so the finished part
    is always a run from byte 0 plus a short window. That run is hashed as it completes (read back from the disk, still in the
    cache), so the SHA-256 is ready when the last piece lands: no second pass over a 400 GB file. Every 206 must carry the
    ETag and total size of the first; another ETag starts the file over, and a 200 to a Range goes back to one connection.
    The .json then says "parallel": true, and bytes_done is the hashed run: a resume keeps that and fetches the window again.
  - Nothing here runs unless the person started or resumed a download (or opened setup: one GET of the manifest).

Tests pass their own `allowed` and `context` to reach a local test server. The product never does.
"""
import errno
import hashlib
import http.client
import json
import os
import select
import socket
import ssl
import sys
import threading
import time
import weakref
import urllib.parse
from pathlib import Path

from engine import distribution   # noqa: E402  (a fork's own download site: engine/distribution.py)

ALLOWED_HOSTS = frozenset({
    'downloads.lastbook.org',
    'huggingface.co',
    'cas-bridge.xethub.hf.co',
}) | distribution.LIBRARY_HOSTS | distribution.MODEL_HOSTS | ({distribution.UPDATE_HOST} - {None})
ALLOWED_SUFFIXES = ('.hf.co',)      # Hugging Face's download hosts (cas-bridge.xethub.hf.co and its siblings)
MAX_REDIRECTS = 5
TIMEOUT = 30                        # seconds without a byte before the connection counts as broken
CHUNK = 1024 * 1024
SAVE_EVERY = 2.0                    # seconds between saves of the partial file's .json
NETWORK_TRIES = 6                   # broken connections in a row, with no progress between them, before giving up
BACKOFF = (2, 4, 8, 15, 30, 30)
BUSY_BACKOFF = (5, 15, 30, 60, 120, 300)   # a server that answers 5xx or 429 is up but busy: wait longer, and keep at it
BUSY_LIMIT = 40 * 60                # seconds of waiting on a busy server, with no progress, before giving up
MAX_RETRY_AFTER = 600               # a Retry-After longer than this is not waited out in one go
USER_AGENT = 'Lastbook-setup/1'
CONNECTIONS = 8                     # at once, for one large file. One connection to downloads.lastbook.org gave ~2 MB/s, 8 ~13 MB/s
PARALLEL_MIN = 512 * 2 ** 20        # smaller files come over one connection, as before
PIECE = 16 * 2 ** 20                # bytes asked for in one Range
AHEAD = 2 * CONNECTIONS             # pieces handed out past the first unfinished one: the most the .part runs ahead of bytes_done


class Stopped(Exception):
    """The person paused or cancelled. .why is 'paused' or 'cancelled'."""

    def __init__(self, why):
        super().__init__(why)
        self.why = why


class Failed(Exception):
    """A plain sentence for the person, and a code from docs/SETUP-API.md."""

    def __init__(self, code, message, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


class BadChecksum(Failed):
    def __init__(self, message):
        super().__init__('checksum', message)


def disk_full(e):
    return isinstance(e, OSError) and (e.errno in (errno.ENOSPC, getattr(errno, 'EDQUOT', -1)) or getattr(e, 'winerror', None) in (112, 39))


def host_allowed(host, allowed=None):
    host = (host or '').lower().rstrip('.')
    if allowed is not None:
        return host in allowed
    return host in ALLOWED_HOSTS or any(host.endswith(s) and len(host) > len(s) for s in ALLOWED_SUFFIXES)


def url_allowed(url, allowed=None):
    """An https address on an allowed host, with no user name or password in it, on port 443 (a test's own hosts: any port)."""
    try:
        u = urllib.parse.urlsplit(url)
        port = u.port
    except ValueError:
        return False
    if u.scheme != 'https' or not u.hostname or u.username or u.password:
        return False
    if allowed is None and port not in (None, 443):
        return False
    return host_allowed(u.hostname, allowed)


def roots_path():
    """engine/setup/roots.pem, beside this file (in the packaged app, where the packaging puts it: engine/setup/ under _MEIPASS)."""
    here = Path(__file__).with_name('roots.pem')
    if not here.is_file() and getattr(sys, 'frozen', False):
        here = Path(getattr(sys, '_MEIPASS', '')) / 'engine' / 'setup' / 'roots.pem'
    return here


def tls_context():
    """TLS 1.2 or later, hostname checked, and trust in the roots of roots.pem only: never the system's store (see
    packaging/setup_roots.py for why and which). With the file missing, nothing is downloaded."""
    roots = roots_path()
    if not roots.is_file():
        raise Failed('network', 'This copy of Lastbook is missing its list of trusted download servers, so it cannot download. '
                                'Download Lastbook again.')
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)          # check_hostname on and CERT_REQUIRED; no default certificates loaded
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cafile=str(roots))
    return ctx


class Control:
    """Pause and cancel, from any thread. The download notices within one chunk or one second of waiting."""

    def __init__(self):
        self.ev = threading.Event()
        self.why = None

    def stop(self, why):
        if self.why != 'cancelled':
            self.why = why
        self.ev.set()

    def check(self):
        if self.ev.is_set():
            raise Stopped(self.why or 'paused')

    def sleep(self, s):
        if self.ev.wait(s):
            raise Stopped(self.why or 'paused')


class _StoppableConnection(http.client.HTTPSConnection):
    """A piece's connection, which a pause can end while it is still connecting or in the TLS handshake. The stock connect() blocks
    in both until the timeout, and only sets .sock once the handshake is over, so there is nothing to shut down before then.
    Here .sock is set as soon as each socket exists, and both steps wait in short slices, checking `stopping` between them."""

    SLICE = 0.25

    def __init__(self, host, port, timeout, context, stopping):
        super().__init__(host, port, timeout=timeout, context=context)
        self.stopping = stopping

    def _check(self):
        if self.stopping.is_set():
            raise Stopped('paused')

    def _wait(self, sock, write, deadline):
        """Until `sock` can be read (or written), a slice at a time. The third list is where Windows reports a refused connect."""
        while True:
            self._check()
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError('timed out')
            r, w, x = select.select([] if write else [sock], [sock] if write else [], [sock], min(self.SLICE, left))
            if r or w or x:
                return

    def connect(self):
        deadline = time.monotonic() + self.timeout
        err = None
        for family, kind, proto, _, addr in socket.getaddrinfo(self.host, self.port, 0, socket.SOCK_STREAM):
            self._check()
            s = socket.socket(family, kind, proto)
            self.sock = s                                   # from here a pause can shut it down
            try:
                s.setblocking(False)
                code = s.connect_ex(addr)
                if code not in (0, errno.EINPROGRESS, errno.EWOULDBLOCK, getattr(errno, 'WSAEWOULDBLOCK', errno.EWOULDBLOCK)):
                    raise OSError(code, os.strerror(code))
                self._wait(s, True, deadline)
                code = s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if code:
                    raise OSError(code, os.strerror(code))
                break
            except Stopped:
                s.close()
                raise
            except OSError as e:
                err = e
                s.close()
                self.sock = None
        else:
            raise err or OSError('no address for the download server')
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        ss = self._context.wrap_socket(s, server_hostname=self.host, do_handshake_on_connect=False)
        self.sock = ss                                      # the wrapped socket replaces the plain one, which it took over
        try:
            while True:
                self._check()                               # also catches a pause that shut the plain socket just before
                try:
                    ss.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    self._wait(ss, False, deadline)
                except ssl.SSLWantWriteError:
                    self._wait(ss, True, deadline)
            ss.settimeout(self.timeout)
        except BaseException:
            ss.close()
            raise


def _cancellable_body(conn, response, stopping, timeout):
    """Keep TLS body reads cancellable without timing out the buffered reader on every poll."""
    raw = getattr(response.fp, 'raw', None)
    sock = conn.sock or getattr(raw, '_sock', None)  # HTTP/1.0 can transfer ownership to the response.
    if sock is None:
        return
    receive = type(sock).recv_into
    socket_ref = weakref.ref(sock)
    sock.setblocking(False)

    def recv_into(*args, **kwargs):
        deadline = time.monotonic() + timeout
        while True:
            if stopping.is_set():
                raise Stopped('paused')
            current = socket_ref()
            if current is None:
                raise OSError('download connection closed')
            try:
                return receive(current, *args, **kwargs)  # Drain decrypted TLS bytes before selecting.
            except (ssl.SSLWantReadError, ssl.SSLWantWriteError) as error:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('timed out')
                write = isinstance(error, ssl.SSLWantWriteError)
                select.select([] if write else [current], [current] if write else [], [current], min(.1, remaining))

    sock.recv_into = recv_into


def _open(url, start, etag, allowed, context, timeout, end=None, opened=None, stopping=None):
    """(connection, response, final url) for a GET from byte `start` (to byte `end`, included, when given), after following
    allowed redirects. `opened(conn)`, when given, hears of each connection, so another thread can close it. With `stopping`
    (an Event), connecting and the TLS handshake end soon after it is set, raising Stopped, rather than at the timeout."""
    for _ in range(MAX_REDIRECTS + 1):
        if not url_allowed(url, allowed):
            raise Failed('network', 'Lastbook refused to download from an address it does not know.')
        u = urllib.parse.urlsplit(url)
        if stopping is not None:
            conn = _StoppableConnection(u.hostname, u.port or 443, timeout, context, stopping)
        else:
            conn = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=timeout, context=context)
        if opened:
            opened(conn)
        headers = {'User-Agent': USER_AGENT, 'Accept-Encoding': 'identity'}
        if end is not None:
            headers['Range'] = f'bytes={start}-{end}'
            if etag:
                headers['If-Range'] = etag
        elif start:
            headers['Range'] = f'bytes={start}-'
            if etag:
                headers['If-Range'] = etag
        conn.request('GET', (u.path or '/') + ('?' + u.query if u.query else ''), headers=headers)
        r = conn.getresponse()
        if stopping is not None:
            _cancellable_body(conn, r, stopping, timeout)
        if r.status in (301, 302, 303, 307, 308):
            loc = r.getheader('Location')
            r.close()
            conn.close()
            if not loc:
                raise Failed('network', 'The download server answered with a move to nowhere.')
            url = urllib.parse.urljoin(url, loc)
            continue
        return conn, r, url
    raise Failed('network', 'The download server sent Lastbook round in circles.')


def fetch_small(url, limit=1 << 20, allowed=None, context=None, timeout=10):
    """One small GET (the manifest). Bytes, or None on any failure."""
    try:
        conn, r, _ = _open(url, 0, None, allowed, context or tls_context(), timeout)
        try:
            if r.status != 200:
                return None
            data = r.read(limit + 1)
            return data if len(data) <= limit else None
        finally:
            conn.close()
    except (OSError, http.client.HTTPException, Failed, ValueError):
        return None


def _load_meta(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_meta(path, d):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f)
    os.replace(tmp, path)


def _content_range(v):
    """(start, total) from 'bytes 100-199/1000', or None."""
    try:
        unit, _, rest = (v or '').partition(' ')
        span, _, total = rest.partition('/')
        return int(span.split('-')[0]), int(total)
    except ValueError:
        return None


class _DiskWriteError(OSError):
    """A write failed after possibly changing the partial file."""


def _write_all(f, data):
    """Write every byte, including short raw writes (#122); preserve disk errors."""
    view = memoryview(data)
    try:
        while view:
            n = f.write(view)
            if not n:
                raise OSError(errno.EIO, 'the disk took no bytes')
            view = view[n:]
    except OSError as e:
        raise _DiskWriteError(e.errno, e.strerror) from e


class Download:
    """One file: <dest>.part grows, with <dest>.part.json beside it (url, size, sha256, bytes done, ETag, and "parallel": true
    while pieces may lie past bytes done). run() returns once the whole file is there and its SHA-256 matches, renamed to <dest>.
    Raises Stopped, Failed or BadChecksum. progress(done, total, phase) is called as bytes arrive; phase is 'downloading' or
    'verifying'. waiting(seconds) is called before each wait on a busy server, over one connection or several. `connections`
    (default CONNECTIONS) is how many at once for a file of PARALLEL_MIN or more; 1 means one."""

    def __init__(self, url, dest, size, sha256, control, progress=lambda d, t, p: None, allowed=None, context=None, timeout=TIMEOUT,
                 waiting=lambda s: None, connections=None):
        self.url, self.dest, self.size, self.sha256 = url, str(dest), int(size), sha256.lower()
        self.part, self.meta = self.dest + '.part', self.dest + '.part.json'
        self.control, self.progress, self.waiting = control, progress, waiting
        self.allowed, self.context, self.timeout = allowed, context, timeout
        self.connections = connections

    def done_bytes(self):
        """Bytes from 0 that are surely the file's: the .part's length, or after pieces, the hashed run the .json records."""
        try:
            n = min(os.path.getsize(self.part), self.size)
        except OSError:
            return 0
        meta = _load_meta(self.meta)
        if meta.get('parallel'):
            b = meta.get('bytes_done')
            n = min(n, b) if isinstance(b, int) and b >= 0 else 0
        return n

    def _parallel_wanted(self):
        n = CONNECTIONS if self.connections is None else self.connections
        return n > 1 and self.size >= PARALLEL_MIN

    def discard(self):
        for p in (self.part, self.meta, self.meta + '.tmp'):
            try:
                os.unlink(p)
            except OSError:
                pass

    def _rehash(self, n):
        """The SHA-256 of the first n bytes already on disk."""
        h = hashlib.sha256()
        done = 0
        with open(self.part, 'rb') as f:
            while done < n:
                self.control.check()
                b = f.read(min(8 * CHUNK, n - done))
                if not b:
                    break
                h.update(b)
                done += len(b)
                self.progress(done, n, 'verifying')
        return h, done

    def run(self):
        if os.path.isfile(self.dest) and os.path.getsize(self.dest) == self.size and not os.path.exists(self.part):
            return self.dest     # finished and checked before a restart
        os.makedirs(os.path.dirname(self.dest) or '.', exist_ok=True)
        meta = _load_meta(self.meta)
        if meta and (meta.get('sha256') != self.sha256 or meta.get('size') != self.size):
            self.discard()       # left by another version of this file
            meta = {}
        ctx = self.context or tls_context()
        start = self.done_bytes()
        if start and not meta.get('etag'):
            start = 0            # nothing to tell the server which file those bytes came from
        with open(self.part, 'ab') as f:
            f.truncate(start)
        h, start = self._rehash(start) if start else (hashlib.sha256(), 0)
        etag = meta.get('etag')
        if start < self.size and self._parallel_wanted():
            h, start, etag = self._run_parallel(h, start, etag, ctx)
        tries = busy = waited = 0       # broken connections, busy answers, and seconds waited on them, with no progress
        ignored_once = False            # a 200 to a Range already asked about since the last progress
        while start < self.size:
            self.control.check()
            try:
                got = self._fetch(start, etag, h, ctx)
                start, etag = got
                tries = busy = waited = 0
                ignored_once = False
            except Stopped:
                raise
            except Failed:
                raise
            except _Restart as r:
                if r.whole and not ignored_once:      # one 200 to a Range can be a passing hiccup at the edge: ask once more first
                    ignored_once = True
                    print(f'setup: the server sent the whole of {os.path.basename(self.dest)} to a resume; asking again', flush=True)
                    self.control.sleep(1)
                    continue
                print(f'setup: the file on the server changed or ignores Range; starting {os.path.basename(self.dest)} over', flush=True)
                with open(self.part, 'r+b') as f:
                    f.truncate(0)
                h, start, etag = hashlib.sha256(), 0, None
                continue
            except _Busy as b:
                now = self.done_bytes()
                if now > start:
                    tries = busy = waited = 0
                    ignored_once = False
                start = now
                etag = _load_meta(self.meta).get('etag') or etag
                wait = min(max(BUSY_BACKOFF[min(busy, len(BUSY_BACKOFF) - 1)], b.wait), BUSY_LIMIT - waited)
                if wait <= 0:
                    raise Failed('server', f'The download server stayed busy ({b}). Try again later.')
                busy += 1
                waited += wait
                print(f'setup: the server is busy at {start} of {self.size} ({b}); trying again in {wait}s', flush=True)
                self.waiting(wait)
                self.control.sleep(wait)
            except ssl.SSLCertVerificationError:
                raise Failed('network', 'Lastbook could not confirm it is talking to the download server, so it stopped. '
                                        'A network that intercepts secure connections (some offices, hotels) can cause this.')
            except (OSError, http.client.HTTPException, ValueError) as e:
                if disk_full(e):
                    raise Failed('no_space', 'The disk filled up while downloading. Free some space, then try again.')
                now = self.done_bytes()
                if isinstance(e, _DiskWriteError):
                    # The OS may have written a prefix before raising. Rebuild the hash from those actual bytes.
                    h, now = self._rehash(now)
                if now > start:
                    tries = busy = waited = 0
                    ignored_once = False
                else:
                    tries += 1
                start = now
                etag = _load_meta(self.meta).get('etag') or etag
                if tries >= NETWORK_TRIES:
                    raise Failed('network', f'The connection kept breaking ({type(e).__name__}). Check the internet connection, then try again.')
                print(f'setup: download broke at {start} of {self.size}: {type(e).__name__}: {e}; trying again', flush=True)
                self.control.sleep(BACKOFF[min(tries, len(BACKOFF) - 1)])
        if h.hexdigest() != self.sha256:
            self.discard()
            raise BadChecksum('The downloaded file did not match its published checksum.')
        os.replace(self.part, self.dest)
        try:
            os.unlink(self.meta)
        except OSError:
            pass
        return self.dest

    def _fetch(self, start, etag, h, ctx):
        """Reads until the end or a break. Returns (bytes done, etag). Raises _Restart when the server's file is not the one begun."""
        conn, r, final = _open(self.url, start, etag, self.allowed, ctx, self.timeout)
        try:
            if r.status == 416 or r.status >= 400:
                if 500 <= r.status < 600 or r.status == 429:
                    raise _Busy(r.status, r.getheader('Retry-After'))
                raise Failed('network', f'The download server refused the file (HTTP {r.status}).')
            new_etag = r.getheader('ETag')
            if r.status == 206:
                cr = _content_range(r.getheader('Content-Range'))
                if not cr or cr[0] != start or cr[1] != self.size or (etag and new_etag and new_etag != etag):
                    raise _Restart(new_etag)
            elif r.status == 200:
                if start:
                    raise _Restart(new_etag, whole=True)    # the server sent the whole file again: its copy changed, or it ignores Range
                length = r.getheader('Content-Length')
                if length is not None and length.isdigit() and int(length) != self.size:
                    raise BadChecksum(f'The file on the server is {int(length):,} bytes, not the {self.size:,} Lastbook expects.')
            else:
                raise OSError(f'HTTP {r.status}')
            etag = new_etag or etag
            meta = {'url': self.url, 'size': self.size, 'sha256': self.sha256, 'etag': etag, 'bytes_done': start}
            _save_meta(self.meta, meta)
            saved = time.time()
            with open(self.part, 'r+b', buffering=0) as f:
                f.seek(start)
                while start < self.size:
                    self.control.check()
                    b = r.read1(min(CHUNK, self.size - start))    # what has arrived, up to CHUNK: progress and pause stay quick on a slow line
                    if not b:
                        raise ConnectionError('the connection closed early')
                    _write_all(f, b)
                    h.update(b)
                    start += len(b)
                    self.progress(start, self.size, 'downloading')
                    if time.time() - saved > SAVE_EVERY:
                        f.flush()
                        meta['bytes_done'] = start
                        _save_meta(self.meta, meta)
                        saved = time.time()
                f.flush()
                os.fsync(f.fileno())
            meta['bytes_done'] = start
            _save_meta(self.meta, meta)
            return start, etag
        finally:
            try:
                conn.close()
            except OSError:
                pass


    # ------------------------------------------------------------------------------------------ several connections --
    def _run_parallel(self, h, start, etag, ctx):
        """Pieces until the file is whole: (h, size, etag). Or, when the server will not send pieces, (h, start, etag) for the one
        connection to carry on from, with the .part cut back to the hashed run and the .json saying so."""
        changed = 0
        while True:
            try:
                return self._pieces(h, start, etag, ctx)
            except _Restart as r:
                h, start, etag = r.h, r.start, r.etag     # where the hashed run had got to
                if r.whole or changed:
                    why = 'ignores Range' if r.whole else 'changed its ETag twice'
                    print(f'setup: the server {why} for {os.path.basename(self.dest)}; carrying on over one connection', flush=True)
                    with open(self.part, 'r+b') as f:
                        f.truncate(start)
                    _save_meta(self.meta, {'url': self.url, 'size': self.size, 'sha256': self.sha256, 'etag': etag, 'bytes_done': start})
                    return h, start, etag
                changed += 1
                print(f'setup: the file on the server changed; starting {os.path.basename(self.dest)} over', flush=True)
                with open(self.part, 'r+b') as f:
                    f.truncate(0)
                h, start, etag = hashlib.sha256(), 0, None

    def _pieces(self, h, start, etag, ctx):
        n = CONNECTIONS if self.connections is None else self.connections
        st = _Pieces(start, self.size, etag)
        workers = [threading.Thread(target=self._worker, args=(st, ctx), daemon=True, name=f'download-{i}')
                   for i in range(min(n, st.count))]
        meta = {'url': self.url, 'size': self.size, 'sha256': self.sha256, 'etag': etag, 'bytes_done': start, 'parallel': True}
        _save_meta(self.meta, meta)
        saved = time.time()
        reported = None
        for w in workers:
            w.start()
        try:
            with open(self.part, 'rb') as rf:
                while start < self.size:
                    self.control.check()
                    with st.cv:
                        if st.error is None and not st.done.get(st.first):
                            st.cv.wait(0.5)
                        if st.error is not None:
                            raise st.error
                        ready = []
                        i = st.first
                        while st.done.get(i):
                            ready.append(i)
                            i += 1
                    for i in ready:                 # hash the run that just became whole, in order
                        a, b = st.span(i)
                        rf.seek(a)
                        while a < b:
                            self.control.check()
                            chunk = rf.read(min(8 * CHUNK, b - a))
                            if not chunk:
                                raise OSError('the partial file is shorter than the pieces written to it')
                            h.update(chunk)
                            a += len(chunk)
                        start = b
                        with st.cv:
                            st.done.pop(i)
                            st.got.pop(i, None)
                            st.first = i + 1
                            st.cv.notify_all()
                    with st.cv:
                        inflight = sum(st.got.values())     # pieces not yet in the hashed run, whole or in part
                        announce, st.announce = st.announce, None
                    now = min(start + inflight, self.size)
                    if now != reported:                     # only as bytes arrive: a call with none would wipe the busy countdown
                        self.progress(now, self.size, 'downloading')
                        reported = now
                    if announce:
                        self.waiting(announce)              # from this thread, like progress: every connection waits on a busy server
                    if (ready and time.time() - saved > SAVE_EVERY) or start >= self.size:
                        with open(self.part, 'r+b') as f:      # what bytes_done vouches for is on the disk before it says so
                            os.fsync(f.fileno())
                        meta['etag'], meta['bytes_done'] = st.etag, start
                        _save_meta(self.meta, meta)
                        saved = time.time()
        except _Restart as r:
            r.h, r.start, r.etag = h, start, (st.etag or etag) if r.whole else None
            raise
        finally:
            st.halt(workers)
            if start < self.size:
                meta['etag'], meta['bytes_done'] = st.etag or meta['etag'], start
                try:
                    _save_meta(self.meta, meta)
                except OSError:
                    pass
        return h, start, st.etag

    def _worker(self, st, ctx):
        """Takes pieces in order and fetches each, trying again where a connection broke. Hands any error to the coordinator."""
        try:
            with open(self.part, 'r+b', buffering=0) as f:
                while True:
                    i = st.take()
                    if i is None:
                        return
                    self._piece(st, i, f, ctx)
        except BaseException as e:      # noqa: B036 -- one place to hear it: the thread that hashes, and raises
            st.fail(e)

    def _piece(self, st, i, f, ctx):
        a, b = st.span(i)
        pos, tries, stray = a, 0, False
        while pos < b:
            if st.stopping.is_set():
                return
            try:
                conn, r, _ = _open(self.url, pos, st.etag, self.allowed, ctx, self.timeout, end=b - 1, opened=st.opened,
                                   stopping=st.stopping)
                try:
                    if 500 <= r.status < 600 or r.status == 429:
                        raise _Busy(r.status, r.getheader('Retry-After'))     # up but busy: wait, as over one connection
                    if r.status >= 400:
                        raise Failed('network', f'The download server refused the file (HTTP {r.status}).')
                    if r.status == 200:
                        st.same(r.getheader('ETag'))      # a server that honours If-Range sends 200 when its copy changed
                        if not stray:                     # one 200 to a Range can be a passing hiccup at the edge: ask once more
                            stray = True
                            print(f'setup: the server sent the whole file to the piece at {pos}; asking again', flush=True)
                            st.closed(conn)
                            if st.stopping.wait(1):
                                return
                            continue
                        raise _Restart(r.getheader('ETag'), whole=True)
                    if r.status != 206:
                        raise OSError(f'HTTP {r.status}')
                    cr = _content_range(r.getheader('Content-Range'))
                    if not cr or cr[0] != pos:
                        raise _Restart(r.getheader('ETag'), whole=True)
                    if cr[1] != self.size:
                        raise BadChecksum(f'The file on the server is {cr[1]:,} bytes, not the {self.size:,} Lastbook expects.')
                    st.same(r.getheader('ETag'))
                    f.seek(pos)
                    while pos < b:
                        if st.stopping.is_set():
                            return
                        chunk = r.read1(min(CHUNK, b - pos))
                        if not chunk:
                            raise ConnectionError('the connection closed early')
                        _write_all(f, chunk)
                        f.flush()
                        pos += len(chunk)
                        tries = 0
                        st.progress(i, pos - a)
                finally:
                    st.closed(conn)
            except (_Restart, Failed, Stopped):
                raise
            except _Busy as e:
                if st.stopping.is_set():
                    return
                wait, new = st.busy_wait(e)             # Failed('server') once the shared budget is spent
                if new:
                    print(f'setup: the server is busy at {pos} of {self.size} ({e}); trying again in {wait}s', flush=True)
                while wait > 0:                         # until the shared wait ends, also if another connection extended it
                    if st.stopping.wait(wait):          # Pause, Cancel or another connection's error ends the wait at once
                        return
                    wait = st.busy_rest()
            except ssl.SSLCertVerificationError:
                raise Failed('network', 'Lastbook could not confirm it is talking to the download server, so it stopped. '
                                        'A network that intercepts secure connections (some offices, hotels) can cause this.')
            except (OSError, http.client.HTTPException, ValueError) as e:
                if st.stopping.is_set():
                    return
                if disk_full(e):
                    raise Failed('no_space', 'The disk filled up while downloading. Free some space, then try again.')
                tries += 1
                if tries >= NETWORK_TRIES:
                    raise Failed('network', f'The connection kept breaking ({type(e).__name__}). Check the internet connection, then try again.')
                print(f'setup: piece at {pos} of {self.size} broke: {type(e).__name__}: {e}; trying again', flush=True)
                if st.stopping.wait(BACKOFF[min(tries - 1, len(BACKOFF) - 1)]):
                    return
        st.finish(i)


class _Pieces:
    """What the connections share: which pieces are handed out, done, how far each has got, the ETag, and the first error."""

    def __init__(self, start, size, etag):
        self.base, self.size, self.etag = start, size, etag
        self.count = -(-(size - start) // PIECE)
        self.next = self.first = 0      # piece numbers, counted from `start`
        self.done, self.got = {}, {}
        self.error = None
        self.cv = threading.Condition()
        self.stopping = threading.Event()
        self.conns = set()
        self.busy = self.waited = 0     # busy answers, and seconds waited on them, with no progress: one budget for all connections
        self.resume_at = 0.0            # (monotonic) the end of the wait now on; other connections that hear "busy" join it
        self.announce = None            # a new wait's seconds, for the coordinator to pass to waiting() (and so to the countdown)

    def span(self, i):
        a = self.base + i * PIECE
        return a, min(a + PIECE, self.size)

    def take(self):
        with self.cv:
            while self.next < self.count and self.next - self.first >= AHEAD and not self.stopping.is_set():
                self.cv.wait(0.5)
            if self.next >= self.count or self.stopping.is_set():
                return None
            self.next += 1
            return self.next - 1

    def progress(self, i, n):
        with self.cv:
            if n > self.got.get(i, 0):          # bytes arrived: the busy server's time limit starts again, as over one connection,
                self.busy = 0                   # but the rest of a wait still on comes after this progress, so it counts
                self.waited = max(0.0, self.resume_at - time.monotonic())
            self.got[i] = n

    def busy_wait(self, b):
        """(seconds, new) for a connection that heard 5xx or 429 (b: _Busy). The rule of the one-connection path: BUSY_BACKOFF,
        or the server's Retry-After when longer, within BUSY_LIMIT of waiting with no progress. But one wait at a time: a
        connection that hears "busy" while a wait is on joins it, so eight connections waiting together count the time once.
        A joining answer whose Retry-After reaches past the wait extends it (only the extension is charged, within the budget).
        new: this call started or extended the wait (it is announced). Raises Failed('server') once the budget is spent."""
        with self.cv:
            now = time.monotonic()
            if self.resume_at > now:
                extra = min(now + b.wait - self.resume_at, BUSY_LIMIT - self.waited)
                if extra <= 0:
                    return self.resume_at - now, False
                self.waited += extra
                self.resume_at += extra
                self.announce = self.resume_at - now
                self.cv.notify_all()
                return self.resume_at - now, True
            wait = min(max(BUSY_BACKOFF[min(self.busy, len(BUSY_BACKOFF) - 1)], b.wait), BUSY_LIMIT - self.waited)
            if wait <= 0:
                raise Failed('server', f'The download server stayed busy ({b}). Try again later.')
            self.busy += 1
            self.waited += wait
            self.resume_at = now + wait
            self.announce = wait
            self.cv.notify_all()
            return wait, True

    def busy_rest(self):
        """Seconds still to wait on the shared wait (another connection may have extended it), or 0."""
        with self.cv:
            return max(0.0, self.resume_at - time.monotonic())

    def finish(self, i):
        with self.cv:
            self.done[i] = True        # its bytes stay counted in .got until the hashed run reaches it
            self.cv.notify_all()

    def same(self, etag):
        """The first ETag seen is the file's; a piece with another is from another copy of it."""
        with self.cv:
            if etag and self.etag and etag != self.etag:
                raise _Restart(etag)
            self.etag = self.etag or etag

    def fail(self, e):
        with self.cv:
            if self.error is None and not self.stopping.is_set():
                self.error = e
            self.cv.notify_all()

    def opened(self, conn):
        with self.cv:
            self.conns.add(conn)
            if self.stopping.is_set():
                raise Stopped('paused')

    def closed(self, conn):
        with self.cv:
            self.conns.discard(conn)
        try:
            conn.close()
        except OSError:
            pass

    def halt(self, workers):
        """Stops every connection at once: a read waiting on the network ends now, not at the timeout. A connection still
        connecting or in its handshake notices `stopping` within a slice. Returns only once every worker has ended, so
        nothing holds the .part open or writes to it after a pause."""
        with self.cv:
            self.stopping.set()
            conns = list(self.conns)
            self.cv.notify_all()
        for c in conns:
            try:
                if c.sock:
                    c.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        for w in workers:
            w.join()        # each wait in a worker is bounded: by `stopping`, a shut socket, or the network timeout


class _Restart(Exception):
    def __init__(self, etag, whole=False):
        super().__init__('restart')
        self.etag, self.whole = etag, whole
        self.h, self.start = None, 0


class _Busy(Exception):
    """The server answered, but with 5xx or 429: the internet is fine, the server is not ready. .wait is its Retry-After, or 0."""

    def __init__(self, status, retry_after):
        super().__init__(f'HTTP {status}')
        try:
            self.wait = max(0, min(int(retry_after or 0), MAX_RETRY_AFTER))
        except ValueError:
            self.wait = 0           # an HTTP date, or nonsense: use the backoff alone
