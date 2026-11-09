#!/usr/bin/env python3
"""radio-relay: makes Synology Audio Station internet radio start instantly, live.

Problem (measured 2026-11-09 on Audio Station 7.2.0-5516 and 7.2.1-6006):
  proxy.cgi sends nothing to the player until it has received more than 4 MiB
  from the station -> 1.7 to 17 minutes of silence, so radio sounds "dead".

Fix: Audio Station talks to this relay instead of the station.
  * Format probes (short reads, no Icy-MetaData) get a tiny silent MP3 at once.
  * The playback request (Icy-MetaData: 1) first gets ~4.06 MiB of padding as
    66 consecutive 64 KiB ID3v2 tags (decoders skip them; one single 4 MiB tag
    is rejected by DS audio on Android), then the live station as MP3.
    MP3 stations are passed through untouched; AAC/AAC+/other are converted to
    MP3 with ffmpeg (browsers will not start AAC behind the padding).
  * ICY song titles are kept.
  * At most RELAY_MAX_ACTIVE playbacks run; starting a new one closes the
    oldest, which also ends orphaned proxy.cgi processes Audio Station leaves.

Endpoints
  /s?u=<url-encoded station URL>        relay one station
  /sbin/tunein-station.pls?id=N         stand-in for yp.shoutcast.com (also .m3u)
  /web/<name>                           station from web_stations.json for a browser <audio> (no padding)
  /web/<name>.json                      now playing for that station
  /health                               "ok" + active streams
"""
import http.client
import os
import queue
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get('RELAY_PORT', '18570'))


def _detect_lan_ip():
    """Primary LAN IPv4 of this host (no packets are sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('192.0.2.1', 9))
        return s.getsockname()[0]
    except OSError:
        return '127.0.0.1'
    finally:
        s.close()


def _first_nameserver():
    try:
        for line in open('/etc/resolv.conf'):
            parts = line.split()
            if len(parts) >= 2 and parts[0] == 'nameserver' and not parts[1].startswith('127.'):
                return parts[1]
    except OSError:
        pass
    return ''


# Must be the NAS LAN address: Audio Station's proxy.cgi refuses URLs that resolve
# to 127.0.0.1 (error 101), so playlists must point at the LAN IP.
NAS_IP = os.environ.get('NAS_IP') or _detect_lan_ip()
PUBLIC_BASE = (os.environ.get('RELAY_PUBLIC') or 'http://%s:%d' % (NAS_IP, PORT)).rstrip('/')
PAD_BYTES = int(os.environ.get('RELAY_PAD', str(4 * 1024 * 1024 + 64 * 1024)))
# Padding variants (2026-11-09 tests through Audio Station):
#   id3multi = 66 consecutive 64 KiB ID3 tags -> plays in Chrome AND DS audio on Android (default)
#   id3      = one 4 MiB ID3 tag             -> Chrome OK, Android DS audio rejects + retries
#   id3v23   = one 4 MiB ID3v2.3 tag         -> same as id3
#   meta/title = padding via ICY metadata    -> proxy.cgi stalls, unusable
PAD_MODE = os.environ.get('RELAY_PAD_MODE', 'id3multi')
META_PAD_BLOCKS = (PAD_BYTES // 4081) + 1
META_INT = int(os.environ.get('RELAY_META_INT', '64'))
MAX_ACTIVE = int(os.environ.get('RELAY_MAX_ACTIVE', '3'))
MP3_BITRATE = os.environ.get('RELAY_MP3_BITRATE', '192k')
FFMPEG = os.environ.get('RELAY_FFMPEG', 'ffmpeg')
YP_HOST = 'yp.shoutcast.com'
YP_IP = os.environ.get('RELAY_YP_IP', '')
# No "Mozilla" here: Shoutcast DNAS servers answer browser-like agents on "/" with
# their HTML status page instead of audio (measured: 90.7 Magic FM, WUSB, J-Pop Sakura).
UA = 'radio-relay/1.10 (Synology Audio Station fix)'
VERSION = '1.10'
CHUNK = 8192
MP3_TYPES = ('audio/mpeg', 'audio/mp3', 'audio/mpeg3', 'audio/x-mpeg')
_ctx = ssl.create_default_context()


import collections
_recent = collections.deque(maxlen=400)


def log(*a):
    line = time.strftime('%Y-%m-%d %H:%M:%S ') + ' '.join(str(x) for x in a)
    _recent.append(line)
    sys.stdout.write(line + '\n')
    sys.stdout.flush()


def id3_padding(total):
    body = total - 10
    ss = bytes([(body >> 21) & 0x7F, (body >> 14) & 0x7F, (body >> 7) & 0x7F, body & 0x7F])
    return b'ID3\x04\x00\x00' + ss + b'\x00' * body


PAD = id3_padding(PAD_BYTES)


def id3_padding_v(total, version):
    body = total - 10
    ss = bytes([(body >> 21) & 0x7F, (body >> 14) & 0x7F, (body >> 7) & 0x7F, body & 0x7F])
    return b'ID3' + bytes([version, 0, 0]) + ss + b'\x00' * body


PADS = {
    'id3': PAD,
    'id3v23': id3_padding_v(PAD_BYTES, 3),
    # many small consecutive ID3 tags instead of one huge one
    'id3multi': id3_padding(64 * 1024) * (PAD_BYTES // (64 * 1024) + 1),
}


def make_silence():
    """~3 s of silent MP3 for format probes (falls back to raw silent frames)."""
    try:
        out = subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                              'anullsrc=r=44100:cl=stereo', '-t', '3', '-c:a', 'libmp3lame',
                              '-b:a', '128k', '-f', 'mp3', 'pipe:1'], capture_output=True, timeout=20).stdout
        if len(out) > 20000:
            return out
    except Exception as e:
        log('silence via ffmpeg failed:', e)
    # MPEG-1 Layer III 128 kbps 44.1 kHz stereo frame header + zero payload = digital silence
    frame = b'\xff\xfb\x90\x64' + b'\x00' * 413
    return frame * 120


SILENCE = make_silence()


# ---------------------------------------------------------------- DNS for yp.shoutcast.com
# /etc/hosts on the NAS points yp.shoutcast.com at this relay, so the relay must
# look up the real address itself, straight from public DNS.

_yp_cache = {'ip': None, 'until': 0}


def _dns_a(name, server):
    import random
    import struct
    tid = random.randint(0, 65535)
    q = struct.pack('>HHHHHH', tid, 0x0100, 1, 0, 0, 0)
    q += b''.join(bytes([len(p)]) + p.encode() for p in name.split('.')) + b'\x00' + struct.pack('>HH', 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(3)
    try:
        s.sendto(q, (server, 53))
        r = s.recv(1500)
    finally:
        s.close()
    an = struct.unpack('>H', r[6:8])[0]
    i = len(q)
    for _ in range(an):
        if r[i] & 0xC0 == 0xC0:
            i += 2
        else:
            while r[i]:
                i += r[i] + 1
            i += 1
        typ, _cls, ttl, ln = struct.unpack('>HHIH', r[i:i + 10])
        i += 10
        if typ == 1 and ln == 4:
            return socket.inet_ntoa(r[i:i + 4]), max(60, min(ttl, 3600))
        i += ln
    raise IOError('no A record for %s from %s' % (name, server))


def yp_ip():
    if YP_IP:
        return YP_IP
    if _yp_cache['ip'] and time.time() < _yp_cache['until']:
        return _yp_cache['ip']
    last = None
    # Raw DNS query to the LAN resolver (bypasses /etc/hosts), then DNS-over-HTTPS.
    for server in [s for s in (os.environ.get('RELAY_DNS') or _first_nameserver()).split(',') if s]:
        try:
            ip, ttl = _dns_a(YP_HOST, server)
            _yp_cache.update(ip=ip, until=time.time() + ttl)
            return ip
        except Exception as e:
            last = e
    import json
    for doh in ('https://1.1.1.1/dns-query?name=%s&type=A', 'https://dns.google/resolve?name=%s&type=A'):
        try:
            req = urllib.request.Request(doh % YP_HOST, headers={'accept': 'application/dns-json'})
            ans = json.load(urllib.request.urlopen(req, timeout=5, context=_ctx)).get('Answer', [])
            ips = [a['data'] for a in ans if a.get('type') == 1]
            if ips:
                _yp_cache.update(ip=ips[0], until=time.time() + 300)
                return ips[0]
        except Exception as e:
            last = e
    if _yp_cache['ip']:
        return _yp_cache['ip']
    raise IOError('cannot resolve %s: %s' % (YP_HOST, last))


# ---------------------------------------------------------------- upstream

class _IcyFix:
    def __init__(self, f):
        self.f, self.first = f, True

    def readline(self, *a):
        line = self.f.readline(*a)
        if self.first:
            self.first = False
            if line.startswith(b'ICY '):
                line = b'HTTP/1.0 ' + line[4:]
        return line

    def __getattr__(self, n):
        return getattr(self.f, n)


def open_upstream(url, want_meta, hops=8):
    for _ in range(hops):
        p = urllib.parse.urlsplit(url)
        host = p.hostname
        port = p.port or (443 if p.scheme == 'https' else 80)
        target = yp_ip() if host == YP_HOST else host
        if p.scheme == 'https':
            conn = http.client.HTTPSConnection(target, port, timeout=20, context=_ctx)
        else:
            conn = http.client.HTTPConnection(target, port, timeout=20)
        path = (p.path or '/') + ('?' + p.query if p.query else '')
        conn.putrequest('GET', path, skip_host=True, skip_accept_encoding=True)
        conn.putheader('Host', host if not p.port else '%s:%d' % (host, p.port))
        conn.putheader('User-Agent', UA)
        conn.putheader('Accept', '*/*')
        if want_meta:
            conn.putheader('Icy-MetaData', '1')
        conn.endheaders()
        resp = conn.response_class(conn.sock, method='GET')
        resp.fp = _IcyFix(resp.fp)
        resp.begin()
        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader('Location')
            conn.close()
            if not loc:
                raise IOError('redirect without Location')
            url = urllib.parse.urljoin(url, loc)
            continue
        if resp.status != 200:
            conn.close()
            raise IOError('upstream HTTP %d' % resp.status)
        is_playlist = '/sbin/tunein-station' in url
        if not is_playlist and (resp.getheader('Content-Type') or '').lower().startswith('text/html'):
            conn.close()
            raise IOError('station answered with a web page, not audio')
        return conn, resp
    raise IOError('too many redirects')


def upstream_audio(resp, metaint, on_title):
    """Yield audio bytes from upstream with ICY metadata removed."""
    if not metaint:
        while True:
            b = resp.read1(CHUNK)
            if not b:
                return
            yield b
    while True:
        left = metaint
        while left:
            b = resp.read(min(left, CHUNK))
            if not b:
                return
            left -= len(b)
            yield b
        n = resp.read(1)
        if not n:
            return
        if n[0]:
            meta = resp.read(n[0] * 16).rstrip(b'\x00')
            if meta:
                on_title(meta)


# ---------------------------------------------------------------- output

class MetaWriter:
    """Writes audio to the client, inserting ICY metadata every `metaint` bytes."""
    def __init__(self, w, metaint, pad_blocks=0, always_title=None):
        self.w, self.metaint, self.count, self.title = w, metaint, 0, b''
        self.always_title = always_title  # bytes: base title sent in every block (toggles a trailing space)
        self.flip = False
        self.last_title = b''
        self.lock = threading.Lock()
        # "meta" padding: the first `pad_blocks` metadata blocks are full size (4081 bytes).
        # proxy.cgi counts them toward its 4 MiB threshold but strips them before the
        # player, so the player receives clean audio with no padding at all.
        self.pad_blocks = pad_blocks
        self.pad_block = bytes([255]) + b"StreamTitle='';".ljust(255 * 16, b'\x00')

    def set_title(self, t):
        with self.lock:
            self.title = t

    def audio(self, data):
        if not self.metaint:
            self.w.write(data)
            return
        mv = memoryview(data)
        while mv:
            part = mv[:self.metaint - self.count]
            self.w.write(part)
            self.count += len(part)
            mv = mv[len(part):]
            if self.count == self.metaint:
                if self.pad_blocks > 0:
                    self.w.write(self.pad_block)
                    self.pad_blocks -= 1
                    self.count = 0
                    continue
                with self.lock:
                    t, self.title = self.title, b''
                if self.always_title is not None:
                    if t:
                        self.last_title = t
                    base = self.last_title or (b"StreamTitle='" + self.always_title + b"';")
                    self.flip = not self.flip
                    t = base[:-2] + (b" ';" if self.flip else b"';") if base.endswith(b"';") else base
                if t:
                    n = min((len(t) + 15) // 16, 255)
                    self.w.write(bytes([n]) + t[:n * 16].ljust(n * 16, b'\x00'))
                else:
                    self.w.write(b'\x00')
                self.count = 0


# ---------------------------------------------------------------- active stream limit

_active = []
_active_lock = threading.Lock()


class Active:
    def __init__(self, url):
        self.url, self.t0, self.closers = url, time.time(), []

    def close(self):
        for c in self.closers:
            try:
                c()
            except Exception:
                pass


def register(act):
    with _active_lock:
        while len(_active) >= MAX_ACTIVE:
            old = _active.pop(0)
            log('closing oldest stream to stay under limit:', old.url[:70])
            old.close()
        _active.append(act)


def unregister(act):
    with _active_lock:
        if act in _active:
            _active.remove(act)


# ---------------------------------------------------------------- web broadcast
# /web/<name> plays a station in a normal browser <audio> element (e.g. a website's
# background music behind an HTTPS reverse proxy). Only stations listed in
# web_stations.json ({"name": "http://station-url"}) are served, so this is not an
# open proxy. One upstream connection per station is shared by every listener, no
# padding, no ICY metadata in the output, and it does not count toward MAX_ACTIVE.

WEB_STATIONS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'web_stations.json')
WEB_BURST = 96 * 1024      # recent audio a new listener gets at once, so playback starts fast
WEB_LINGER = 20            # seconds the upstream stays open after the last listener leaves
_web = {}
_web_lock = threading.Lock()


def web_stations():
    import json
    try:
        with open(WEB_STATIONS_FILE, encoding='utf-8') as f:
            d = json.load(f)
        return {k: v for k, v in d.items() if isinstance(v, str) and v.startswith(('http://', 'https://'))}
    except (OSError, ValueError, AttributeError):
        return {}


class Broadcast:
    def __init__(self, name, url):
        self.name, self.url = name, url
        self.lock = threading.Lock()
        self.listeners = set()
        self.burst, self.burst_len = collections.deque(), 0
        self.title, self.icy_name = '', ''
        self.ready, self.error, self.dead = threading.Event(), None, False
        threading.Thread(target=self.run, daemon=True).start()

    def set_title(self, meta):
        t = meta.decode('utf-8', 'replace')
        i = t.find("StreamTitle='")
        if i >= 0:
            t = t[i + 13:]
            j = t.find("';")
            self.title = (t[:j] if j >= 0 else t).strip()

    def add(self):
        q = queue.Queue(maxsize=512)
        with self.lock:
            if self.dead:
                return None
            for b in self.burst:
                q.put_nowait(b)
            self.listeners.add(q)
        return q

    def remove(self, q):
        with self.lock:
            self.listeners.discard(q)

    @staticmethod
    def _end(q):
        with q.mutex:
            q.queue.clear()
        q.put_nowait(None)

    def push(self, b):
        with self.lock:
            self.burst.append(b)
            self.burst_len += len(b)
            while self.burst_len - len(self.burst[0]) >= WEB_BURST:
                self.burst_len -= len(self.burst.popleft())
            for q in list(self.listeners):
                try:
                    q.put_nowait(b)
                except queue.Full:          # listener too slow: drop it, the browser reconnects
                    self.listeners.discard(q)
                    self._end(q)
            return len(self.listeners)

    def run(self):
        conn = ff = None
        t0, why = time.time(), 'eof'
        try:
            conn, resp = open_upstream(self.url, True)
            ctype = (resp.getheader('Content-Type') or 'audio/mpeg').split(';')[0].strip().lower()
            self.icy_name = resp.getheader('icy-name') or self.name
            src = upstream_audio(resp, int(resp.getheader('icy-metaint') or 0), self.set_title)
            if ctype not in MP3_TYPES:
                ff = subprocess.Popen([FFMPEG, '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0',
                                       '-vn', '-c:a', 'libmp3lame', '-b:a', MP3_BITRATE, '-f', 'mp3', 'pipe:1'],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE)

                def feed(src=src):
                    try:
                        for b in src:
                            ff.stdin.write(b)
                    except Exception:
                        pass
                    finally:
                        try:
                            ff.stdin.close()
                        except Exception:
                            pass
                threading.Thread(target=feed, daemon=True).start()
                src = iter(lambda: ff.stdout.read1(CHUNK), b'')
            self.ready.set()
            log('web start', self.name, ctype, self.url[:70])
            idle_since = None
            for b in src:
                if self.push(b):
                    idle_since = None
                elif idle_since is None:
                    idle_since = time.time()
                elif time.time() - idle_since > WEB_LINGER:
                    why = 'idle'
                    break
        except Exception as e:
            self.error, why = e, '%s: %s' % (type(e).__name__, e)
        finally:
            with self.lock:
                self.dead = True
                for q in self.listeners:
                    self._end(q)
                self.listeners.clear()
            with _web_lock:
                if _web.get(self.name) is self:
                    del _web[self.name]
            for c in (conn and conn.close, ff and ff.kill):
                try:
                    c and c()
                except Exception:
                    pass
            self.ready.set()
            log('web stop', self.name, 'secs', round(time.time() - t0, 1), why)


def web_broadcast(name):
    url = web_stations().get(name)
    if not url:
        return None
    for _ in range(3):
        with _web_lock:
            bc = _web.get(name)
            if bc is None or bc.url != url:
                bc = _web[name] = Broadcast(name, url)
        bc.ready.wait(20)
        if bc.error:
            raise IOError('station unavailable: %s' % bc.error)
        q = bc.add()
        if q is not None:
            return bc, q
    raise IOError('station keeps stopping')


# ---------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.0'
    server_version = 'radio-relay/' + VERSION

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        p = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(p.query)
        try:
            if p.path == '/log':
                return self._send(200, 'text/plain; charset=utf-8', ('\n'.join(_recent) + '\n').encode('utf-8', 'replace'))
            if p.path != '/health':
                log('REQ', self.client_address[0], p.path, 'm=' + (qs.get('m') or [PAD_MODE])[0], urllib.parse.unquote(qs.get('u', [''])[0])[:70] or p.query[:40],
                    '| UA=' + (self.headers.get('User-Agent') or '-')[:45],
                    '| icy=' + (self.headers.get('Icy-MetaData') or '-'),
                    '| range=' + (self.headers.get('Range') or '-'))
            if p.path == '/health':
                with _active_lock:
                    body = 'ok\nversion %s\nactive %d\n%s' % (VERSION, len(_active), ''.join(
                        '  %ds %s\n' % (time.time() - a.t0, a.url) for a in _active))
                return self._send(200, 'text/plain', body.encode())
            if p.path.startswith('/sbin/tunein-station'):
                return self._playlist(p)
            if p.path.startswith('/web/'):
                return self._web(p.path[5:])
            if p.path == '/s' and qs.get('u'):
                return self._stream(qs['u'][0], (qs.get('m') or [PAD_MODE])[0], int((qs.get('mi') or [META_INT])[0]))
            return self._send(404, 'text/plain', b'not found\n')
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log('ERROR', self.path[:120], type(e).__name__, e)
            try:
                self._send(502, 'text/plain', ('upstream error: %s\n' % e).encode())
            except Exception:
                pass

    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _web(self, name):
        if name.endswith('.json'):              # now playing: {"name", "title", "listeners"}
            import json
            name = name[:-5]
            if name not in web_stations():
                return self._send(404, 'text/plain', b'not found\n')
            with _web_lock:
                bc = _web.get(name)
            info = {'name': bc.icy_name if bc else name, 'title': bc.title if bc else '',
                    'listeners': len(bc.listeners) if bc else 0}
            return self._send(200, 'application/json', json.dumps(info).encode())
        got = web_broadcast(name)
        if not got:
            return self._send(404, 'text/plain', b'not found\n')
        bc, q = got
        t0, sent = time.time(), 0
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            while True:
                try:
                    b = q.get(timeout=30)
                except queue.Empty:
                    break
                if b is None:
                    break
                self.wfile.write(b)
                sent += len(b)
        finally:
            bc.remove(q)
            log('web listener', name, 'out_bytes', sent, 'secs', round(time.time() - t0, 1))

    def _playlist(self, p):
        conn, resp = open_upstream('http://%s%s?%s' % (YP_HOST, p.path, p.query), False)
        try:
            text = resp.read(65536).decode('latin1', 'replace')
        finally:
            conn.close()
        out = []
        for line in text.splitlines():
            s = line.strip()
            if s.lower().startswith('file') and '=' in s:
                k, v = s.split('=', 1)
                out.append('%s=%s/s?u=%s' % (k, PUBLIC_BASE, urllib.parse.quote(v.strip(), safe='')))
            elif s.startswith(('http://', 'https://')):
                out.append('%s/s?u=%s' % (PUBLIC_BASE, urllib.parse.quote(s, safe='')))
            else:
                out.append(line)
        ctype = 'audio/x-scpls' if p.path.endswith('.pls') else 'audio/x-mpegurl'
        self._send(200, ctype, ('\n'.join(out) + '\n').encode('latin1', 'replace'))
        log('playlist', p.query)

    def _stream(self, url, mode='id3multi', meta_int=64):
        ua = self.headers.get('User-Agent', '')
        icy = self.headers.get('Icy-MetaData') == '1'
        playback = icy or ua.startswith('AudioStation/5')
        if mode in ('meta', 'title') and not icy:
            mode = 'id3multi'
        if not playback:
            # Audio Station format probe: answer instantly with silent MP3.
            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            self.end_headers()
            self.wfile.write(SILENCE)
            return
        conn, resp = open_upstream(url, True)
        ctype = (resp.getheader('Content-Type') or 'audio/mpeg').split(';')[0].strip().lower()
        metaint = int(resp.getheader('icy-metaint') or 0)
        transcode = ctype not in MP3_TYPES
        act = Active(url)
        act.closers.append(conn.close)
        act.closers.append(lambda: self.connection.shutdown(socket.SHUT_RDWR))
        register(act)
        t0, sent, why = time.time(), 0, 'eof'
        ff = None
        try:
            self.send_response(200)
            self.send_header('Content-Type', 'audio/mpeg')
            for h in ('icy-name', 'icy-genre', 'icy-url', 'icy-description'):
                v = resp.getheader(h)
                if v:
                    self.send_header(h, v)
            if not transcode and resp.getheader('icy-br'):
                self.send_header('icy-br', resp.getheader('icy-br'))
            out_metaint = meta_int if mode in ('meta', 'title') else 16000
            self.send_header('icy-metaint', str(out_metaint))
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            if mode == 'title':
                name = (resp.getheader('icy-name') or 'radio').encode('latin1', 'replace')[:100]
                out = MetaWriter(self.wfile, out_metaint, always_title=name)
            elif mode == 'meta':
                out = MetaWriter(self.wfile, out_metaint, pad_blocks=META_PAD_BLOCKS)
            else:
                out = MetaWriter(self.wfile, out_metaint)
                out.audio(PADS.get(mode, PAD))
            src = upstream_audio(resp, metaint, out.set_title)
            if not transcode:
                for b in src:
                    out.audio(b)
                    sent += len(b)
            else:
                ff = subprocess.Popen([FFMPEG, '-hide_banner', '-loglevel', 'error', '-i', 'pipe:0',
                                       '-vn', '-c:a', 'libmp3lame', '-b:a', MP3_BITRATE, '-f', 'mp3', 'pipe:1'],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE)
                act.closers.append(ff.kill)

                def feed():
                    try:
                        for b in src:
                            ff.stdin.write(b)
                    except Exception:
                        pass
                    finally:
                        try:
                            ff.stdin.close()
                        except Exception:
                            pass
                threading.Thread(target=feed, daemon=True).start()
                while True:
                    b = ff.stdout.read1(CHUNK)
                    if not b:
                        break
                    out.audio(b)
                    sent += len(b)
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            why = type(e).__name__
        finally:
            unregister(act)
            act.close()
            if ff:
                ff.kill()
            log('stream', 'm=' + mode, 'mp3->mp3' if not transcode else '%s->mp3' % ctype, url[:70],
                'out_bytes', sent, 'secs', round(time.time() - t0, 1), why)


def watch_for_updates():
    """Restart this process when radio_relay.py changes (only while no stream is playing),
    so fixes can be deployed by editing the file. Syntax errors are refused."""
    path = os.path.abspath(__file__)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return
    while True:
        time.sleep(5)
        try:
            m = os.stat(path).st_mtime
        except OSError:
            continue
        if m == mtime:
            continue
        try:
            compile(open(path, encoding='utf-8').read(), path, 'exec')
        except Exception as e:
            log('update ignored, file does not compile:', e)
            mtime = m
            continue
        while True:
            with _active_lock:
                idle = not _active
            if idle:
                break
            time.sleep(2)
        log('radio_relay.py changed, restarting')
        os.execv(sys.executable, [sys.executable, '-I', path])


def main():
    srv = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    srv.daemon_threads = True
    log('radio-relay', VERSION, 'on port', PORT, 'public', PUBLIC_BASE, 'pad', PAD_BYTES,
        'max_active', MAX_ACTIVE, 'silence_bytes', len(SILENCE))
    threading.Thread(target=watch_for_updates, daemon=True).start()
    srv.serve_forever()


if __name__ == '__main__':
    main()
