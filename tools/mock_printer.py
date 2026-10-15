"""A fake PrusaLink printer (modelled on the Buddy firmware: MK4/MINI/XL/Core One) for development.

    python tools/mock_printer.py --dir some/folder/with/gcodes [--port 8080] [--password secret]

Serves the folder as the "usb" storage, generates thumbnails, simulates a print job, throttles
transfers like a microcontroller would, and logs how many requests were in flight at once.
Files with "nothumb" in their name have no preview. Log in as "maker" (Digest) or send the
password as X-Api-Key.
"""
import argparse
import colorsys
import hashlib
import io
import json
import re
import secrets
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from PIL import Image, ImageDraw

REALM = 'Printer API'
args = None
lock = threading.Lock()
in_flight = max_in_flight = 0
printer = dict(state='IDLE', job=None, nozzle=24.0, bed=23.0, target_nozzle=0.0, target_bed=0.0)


def md5(text):
    return hashlib.md5(text.encode()).hexdigest()


def short_names(folder):
    """Map {8.3 name: real Path} the way FAT does for long names (roughly)."""
    names, counters = {}, {}
    for p in sorted(folder.iterdir()):
        if p.name.startswith('.') or p.suffix == '.part':
            continue
        stem, ext = (p.stem, p.suffix[1:]) if p.is_file() else (p.name, '')
        if len(stem) <= 8 and len(ext) <= 3 and re.fullmatch(r'[A-Za-z0-9_\-]+', stem):
            sfn = p.name.upper()
        else:
            base = re.sub(r'[^A-Z0-9_\-]', '', stem.upper())[:6] or 'FILE'
            counters[base] = counters.get(base, 0) + 1
            sfn = f'{base}~{counters[base]}' + (f'.{ext[:3].upper()}' if ext else '')
        names[sfn] = p
    return names


def resolve(path, must_exist=True):
    """Storage path (/usb/A~1/B~1.GCO, long names also accepted) -> real Path, or None."""
    parts = [p for p in path.split('/') if p]
    if not parts or parts[0] != 'usb':
        return None
    real = args.dir
    for i, part in enumerate(parts[1:]):
        found = short_names(real).get(part.upper()) if real.is_dir() else None
        found = found or (real / part if (real / part).exists() else None)
        if found is None:
            last = i == len(parts) - 2
            return None if must_exist or not last else real / part
        real = found
    return real


def entry(p, sfn):
    st = p.stat()
    e = dict(name=sfn, ro=False, type='FOLDER' if p.is_dir() else 'PRINT_FILE', m_timestamp=int(st.st_mtime))
    if p.is_file():
        rel = '/usb/' + '/'.join(short_name_path(p))
        e['refs'] = dict(icon=f'/thumb/s{rel}', thumbnail=f'/thumb/l{rel}', download=rel)
    e['display_name'] = p.name
    return e


def short_name_path(p):
    out = []
    while p != args.dir:
        out.insert(0, next(s for s, q in short_names(p.parent).items() if q == p))
        p = p.parent
    return out


def thumbnail(p, small):
    hue = int(md5(p.name)[:4], 16) / 0xffff
    w, h = (16, 16) if small else (313, 173)
    im = Image.new('RGB', (w, h), tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.25, 0.25)))
    d = ImageDraw.Draw(im)
    color = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(hue, 0.8, 0.95))
    d.polygon([(w * .3, h * .9), (w * .7, h * .9), (w * .6, h * .15), (w * .4, h * .15)], fill=color)
    if not small:
        d.text((6, 6), p.name[:40], fill='white')
    out = io.BytesIO()
    im.save(out, 'PNG')
    return out.getvalue()


def tick():
    """Advance the simulated printer."""
    now = time.time()
    for k in ('nozzle', 'bed'):
        target = printer['target_' + k] or 23.0
        printer[k] += (target - printer[k]) * 0.1
    job = printer['job']
    if job and printer['state'] == 'PRINTING':
        job['time_printing'] += now - job['last']
        job['progress'] = min(100.0, 100 * job['time_printing'] / args.print_time)
        if job['progress'] >= 100:
            printer.update(state='FINISHED', target_nozzle=0, target_bed=0)
    if job:
        job['last'] = now


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    nonces = {}

    def log_message(self, fmt, *a):
        pass

    # --- plumbing ------------------------------------------------------------------------
    def send(self, code, body=b'', ctype='application/json', headers=()):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
        self.send_response(code)
        if body or code not in (204, 304):
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)
        self.status = code

    def error(self, code, title):
        if self.headers.get('Content-Length') and self.command in ('PUT', 'POST'):
            self.close_connection = True  # like the firmware: answer before swallowing the body
        reason = self.responses.get(code, ('Error',))[0]
        self.send(code, {'title': f'{code}: {reason}', 'message': title if title != reason else ''})

    def authorized(self):
        if args.password and self.headers.get('X-Api-Key') == args.password:
            return True
        auth = self.headers.get('Authorization', '')
        if auth.startswith('Digest '):
            f = dict(re.findall(r'(\w+)="?([^",]*)"?', auth))
            ha1 = md5(f'maker:{REALM}:{args.password}')
            expected = md5(f"{ha1}:{f.get('nonce')}:{md5(f'{self.command}:{self.path}')}")
            fresh = time.time() - self.nonces.get(f.get('nonce'), 0) < 300
            if f.get('response') == expected and fresh:
                return True
        nonce = secrets.token_hex(8)
        self.nonces[nonce] = time.time()
        self.close_connection = self.command == 'PUT'
        self.send(401, {'title': 'Unauthorized'}, headers=[
            ('WWW-Authenticate', f'Digest realm="{REALM}", nonce="{nonce}", stale=false')])
        return False

    def route(self):
        global in_flight, max_in_flight
        with lock:  # count requests being served, not idle keep-alive connections
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            n = in_flight
        try:
            self.status = None
            time.sleep(args.latency)
            self.serve()
            print(f'[{n} in flight, max {max_in_flight}] {self.command} {unquote(self.path)} -> {self.status}',
                  flush=True)
        finally:
            with lock:
                in_flight -= 1

    def serve(self):
        if not self.authorized():
            return
        with lock:
            tick()
        path = unquote(self.path.split('?')[0])
        if path.startswith('/api/v1/files/'):
            return self.files(path[len('/api/v1/files'):])
        if path.startswith('/thumb/'):
            p = resolve(path[8:])
            if not p or not p.is_file():
                return self.error(404, 'Not found')
            if 'nothumb' in p.name:
                return self.error(404, "File doesn't contain preview")
            time.sleep(args.latency * 3)  # the firmware has to dig through the G-code
            return self.send(200, thumbnail(p, path[7] == 's'), 'image/png')
        if path.startswith('/usb/'):
            p = resolve(path)
            return self.stream_file(p) if p and p.is_file() else self.error(404, 'Not found')
        job = printer['job']
        if path == '/api/version':
            return self.send(200, dict(api='2.0.0', server='2.1.2', text='PrusaLink', hostname='mock-mk4',
                                       firmware='6.4.0+mock', capabilities={'upload-by-put': True}))
        if path == '/api/v1/info':
            return self.send(200, dict(nozzle_diameter=0.4, mmu=False, serial='MOCK000001', hostname='mock-mk4',
                                       min_extrusion_temp=170))
        if path == '/api/v1/storage':
            return self.send(200, dict(storage_list=[dict(path='/usb/', name='usb', type='USB', read_only=False,
                                                          available=True)]))
        if path == '/api/v1/status':
            st = dict(storage=dict(path='/usb/', name='usb', read_only=False), printer=dict(
                state=printer['state'], temp_nozzle=round(printer['nozzle'], 1),
                target_nozzle=printer['target_nozzle'], temp_bed=round(printer['bed'], 1),
                target_bed=printer['target_bed'], axis_z=round(0.2 + (job or {}).get('progress', 0) / 4, 1),
                flow=100, speed=100, fan_hotend=4200 if job else 0, fan_print=3100 if job else 0))
            if job:
                st['job'] = dict(id=job['id'], progress=round(job['progress'], 2),
                                 time_printing=int(job['time_printing']),
                                 time_remaining=int(args.print_time - job['time_printing']))
            return self.send(200, st)
        if path == '/api/v1/job':
            if not job:
                return self.send(204)
            p = job['file']
            sfn = '/usb/' + '/'.join(short_name_path(p)) if p.exists() else '/usb/GONE.GCO'
            return self.send(200, dict(id=job['id'], state=printer['state'], progress=job['progress'],
                                       time_printing=int(job['time_printing']), file=dict(
                refs=dict(icon='/thumb/s' + sfn, thumbnail='/thumb/l' + sfn, download=sfn),
                name=sfn.rsplit('/', 1)[1], display_name=p.name, path=sfn.rsplit('/', 1)[0],
                size=p.stat().st_size if p.exists() else 0, m_timestamp=int(p.stat().st_mtime) if p.exists() else 0)))
        if m := re.fullmatch(r'/api/v1/job/(\d+)(?:/(pause|resume))?', path):
            if not job or int(m[1]) != job['id']:
                return self.error(404, 'Not found')
            want = {('PUT', 'pause'): ('PRINTING', 'PAUSED'), ('PUT', 'resume'): ('PAUSED', 'PRINTING'),
                    ('DELETE', None): (None, 'STOPPED')}.get((self.command, m[2]))
            if not want or (want[0] and printer['state'] != want[0]) or \
                    (not want[0] and printer['state'] not in ('PRINTING', 'PAUSED')):
                return self.error(409, 'Conflict')
            printer['state'] = want[1]
            if want[1] == 'STOPPED':
                printer.update(target_nozzle=0, target_bed=0)
            return self.send(204)
        self.error(404, 'Not found')

    do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = route

    # --- files ---------------------------------------------------------------------------
    def files(self, path):
        if self.command == 'PUT':
            return self.upload(path)
        p = resolve(path)
        if p is None:
            return self.error(404, 'Not found')
        if self.command in ('GET', 'HEAD') and p.is_dir():
            children = [entry(q, sfn) for sfn, q in short_names(p).items()]
            etag = '"%s"' % md5(json.dumps(children))[:8]
            if self.headers.get('If-None-Match') == etag:
                return self.send(304, headers=[('ETag', etag)])
            return self.send(200, dict(type='FOLDER', ro=False, name=p.name, children=children),
                             headers=[('ETag', etag)])
        if self.command in ('GET', 'HEAD'):
            e = entry(p, path.rsplit('/', 1)[1].upper())
            e['size'] = p.stat().st_size
            return self.send(200, e)
        if self.command == 'DELETE':
            if printer['job'] and printer['job']['file'] == p and printer['state'] in ('PRINTING', 'PAUSED'):
                return self.error(409, 'File is busy')
            if p.is_dir():
                if any(p.iterdir()) and self.headers.get('Force') != '?1':
                    return self.error(409, 'Folder is not empty')
                import shutil
                shutil.rmtree(p)
            else:
                p.unlink()
            return self.send(204)
        if self.command == 'POST':
            if printer['state'] in ('PRINTING', 'PAUSED'):
                return self.error(409, 'Conflict')
            jid = (printer['job'] or {}).get('id', 100) + 1
            printer.update(state='PRINTING', target_nozzle=215.0, target_bed=60.0, job=dict(
                id=jid, file=p, progress=0.0, time_printing=0.0, last=time.time()))
            return self.send(204)
        self.error(405, 'Method not allowed')

    def upload(self, path):
        p = resolve(path, must_exist=False)
        if p is None:
            return self.error(404, 'Not found')
        if self.headers.get('Create-Folder') == '?1':
            p.mkdir()
            return self.send(201)
        if p.exists() and self.headers.get('Overwrite') != '?1':
            return self.error(409, 'Already exists')
        length = int(self.headers.get('Content-Length', 0))
        tmp = p.with_name(p.name + '.part')
        try:
            with open(tmp, 'wb') as f:
                while length > 0:
                    chunk = self.rfile.read(min(length, 16384))
                    if not chunk:
                        raise ConnectionError('client went away')
                    f.write(chunk)
                    length -= len(chunk)
                    time.sleep(len(chunk) / args.rate)
        except OSError:
            tmp.unlink()
            self.close_connection = True
            return
        tmp.replace(p)
        if self.headers.get('Print-After-Upload') == '?1':
            self.command = 'POST'
            return self.files(path)
        self.send(201)

    def stream_file(self, p):
        size = p.stat().st_size
        self.send_response(200)
        self.send_header('Content-Type', 'text/x.gcode')
        self.send_header('Content-Length', str(size))
        self.end_headers()
        with open(p, 'rb') as f:
            while chunk := f.read(16384):
                self.wfile.write(chunk)
                time.sleep(len(chunk) / args.rate)
        self.status = 200


def make_samples(folder):
    folder.mkdir(parents=True, exist_ok=True)
    names = ['Benchy 0.2mm PLA MK4 1h2m.gcode', 'calibration cube.bgcode', 'Spool holder PETG 3h.gcode',
             'Phone stand v2 0.15mm.gcode', 'nothumb test.gcode', 'clip.gcode']
    for i, name in enumerate(names):
        (folder / name).write_bytes(b'; mock gcode\n' + b'G1 X10 Y10\n' * (20000 * (i + 1)))
    (folder / 'Old prints').mkdir(exist_ok=True)
    for i in range(3):
        (folder / 'Old prints' / f'Archived part {i + 1}.gcode').write_bytes(b'G28\n' * 50000)


def make_server(argv=None):
    global args
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--port', type=int, default=8080)
    ap.add_argument('--dir', type=Path, help='folder served as USB storage (default: temp dir with samples)')
    ap.add_argument('--password', default='secret')
    ap.add_argument('--latency', type=float, default=0.05, help='seconds added to every request')
    ap.add_argument('--rate', type=float, default=400_000, help='transfer speed, bytes/s')
    ap.add_argument('--print-time', type=float, default=600, help='simulated print duration, seconds')
    args = ap.parse_args(argv)
    if not args.dir:
        args.dir = Path(tempfile.mkdtemp(prefix='mock-usb-'))
        make_samples(args.dir)
    args.dir = args.dir.resolve()
    return ThreadingHTTPServer(('127.0.0.1' if argv is not None else '', args.port), Handler)


def main():
    server = make_server()
    print(f'Serving {args.dir} on http://127.0.0.1:{server.server_port} (user "maker", password "{args.password}")')
    server.serve_forever()


if __name__ == '__main__':
    main()
