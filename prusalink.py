"""PrusaLink v1 API client, single-file-at-a-time worker lanes and thumbnail cache.

Nothing in here touches the GUI: workers hand results back through a `post(key, value)` callback.
"""
import hashlib
import os
import re
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote

import requests
from requests.auth import HTTPDigestAuth

TIMEOUT = (5, 60)  # (connect, read) seconds; reads are slow while the printer parses G-code
CHUNK = 64 * 1024


class ApiError(Exception):
    def __init__(self, status, title, message=''):
        self.status = status
        super().__init__(f'{status} {title}' + (f' - {message}' if message and message != title else ''))

    @classmethod
    def from_response(cls, r):
        # Buddy firmware: {"title": "409: Conflict", "message": "File is busy"} or the same as text;
        # PrusaLink on a Raspberry Pi uses {"title", "text"}.
        try:
            data = r.json()
            title, message = data.get('title') or '', data.get('text') or data.get('message') or ''
        except ValueError:
            title, _, message = r.text.partition('\n\n')
        title = re.sub(r'^\d{3}:\s*', '', title.strip())[:100] or r.reason
        if r.status_code == 401:
            message = 'check the username and password'
        return cls(r.status_code, title, message.strip()[:200])


class Cancelled(Exception):
    pass


class Printer:
    """Stateless API wrapper. Each worker thread owns its own `session()`."""

    def __init__(self, host, username='maker', password=''):
        host = host.strip().rstrip('/')
        self.base = host if '://' in host else f'http://{host}'
        self.username, self.password = username, password

    def session(self):
        s = requests.Session()
        s.headers['Accept'] = 'application/json'  # errors come back as JSON instead of text
        if self.username:
            s.auth = HTTPDigestAuth(self.username, self.password)
        elif self.password:  # Buddy firmware accepts its password as an API key
            s.headers['X-Api-Key'] = self.password
        return s

    def request(self, s, method, path, ok=(), **kw):
        kw.setdefault('timeout', TIMEOUT)
        try:
            r = s.request(method, self.base + quote(path), **kw)
        except requests.Timeout as e:
            raise TimeoutError(f'{self.base} did not respond in time') from e
        except requests.ConnectionError as e:
            raise ConnectionError(f'cannot connect to {self.base}') from e
        if r.status_code >= 400 and r.status_code not in ok:
            error = ApiError.from_response(r)
            r.close()
            raise error
        return r

    def get_json(self, s, path):
        r = self.request(s, 'GET', path)
        return r.json() if r.status_code == 200 and r.content else None

    # --- printer & job -------------------------------------------------------------------
    def status(self, s):
        return self.get_json(s, '/api/v1/status')

    def info(self, s):
        return self.get_json(s, '/api/v1/info')

    def job(self, s):
        return self.get_json(s, '/api/v1/job')  # None when there is no job (204)

    def pause(self, s, job_id):
        self.request(s, 'PUT', f'/api/v1/job/{job_id}/pause')

    def resume(self, s, job_id):
        self.request(s, 'PUT', f'/api/v1/job/{job_id}/resume')

    def stop(self, s, job_id):
        self.request(s, 'DELETE', f'/api/v1/job/{job_id}')

    # --- storage -------------------------------------------------------------------------
    # `path` arguments below are storage paths such as "/usb/FOLDER/FILE~1.GCO".
    def storages(self, s):
        return (self.get_json(s, '/api/v1/storage') or {}).get('storage_list', [])

    def listdir(self, s, path, etag=None):
        """Return (children, etag), or None if the listing is unchanged since `etag`."""
        r = self.request(s, 'GET', f'/api/v1/files{path}/', headers={'If-None-Match': etag} if etag else {})
        if r.status_code == 304:
            return None
        return (r.json() if r.content else {}).get('children', []), r.headers.get('ETag')

    def details(self, s, path):
        return self.get_json(s, f'/api/v1/files{path}')

    def exists(self, s, path):
        return self.request(s, 'HEAD', f'/api/v1/files{path}', ok=(404,)).status_code != 404

    def start_print(self, s, path):
        self.request(s, 'POST', f'/api/v1/files{path}')

    def mkdir(self, s, path):
        self.request(s, 'PUT', f'/api/v1/files{path}', headers={'Create-Folder': '?1'})

    def delete(self, s, path, task=None):
        self.request(s, 'DELETE', f'/api/v1/files{path}', headers={'Force': '?1'})

    def thumbnail(self, s, ref):
        """PNG/JPEG/QOI bytes, or b'' if the file has no preview."""
        r = self.request(s, 'GET', ref, ok=(404,), headers={'Accept': 'image/*'})
        return b'' if r.status_code == 404 else r.content

    def download(self, s, ref, dest, task):
        tmp = f'{dest}.part'
        try:
            with self.request(s, 'GET', ref, stream=True) as r, open(tmp, 'wb') as f:
                task.total = int(r.headers.get('Content-Length') or 0) or task.total
                for chunk in r.iter_content(CHUNK):
                    task.check()
                    f.write(chunk)
                    task.done += len(chunk)
            os.replace(tmp, dest)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def upload(self, s, path, src, task, overwrite=False, print_after=False):
        # A cheap request first, so a Digest nonce is negotiated before the body goes out:
        # otherwise the whole file would be sent once just to collect a 401.
        if self.exists(s, path) and not overwrite:
            raise ApiError(409, 'Conflict', 'File already exists')
        headers = {'Content-Type': 'application/gcode+binary' if path.endswith('.bgcode') else 'text/x.gcode'}
        if overwrite:  # Only send these when set: some firmware only looks at header presence
            headers['Overwrite'] = '?1'
        if print_after:
            headers['Print-After-Upload'] = '?1'
        with open(src, 'rb') as f:
            self.request(s, 'PUT', f'/api/v1/files{path}', data=_ProgressReader(f, task), headers=headers,
                         timeout=(TIMEOUT[0], 300))


class _ProgressReader:
    """File wrapper for streamed uploads: gives requests a Content-Length, tracks progress, can cancel."""

    def __init__(self, f, task):
        self.f, self.task = f, task
        task.total = os.fstat(f.fileno()).st_size

    def __len__(self):
        return self.task.total

    def read(self, n=-1):
        self.task.check()
        data = self.f.read(n)
        self.task.done = self.f.tell()
        return data

    def tell(self):
        return self.f.tell()

    def seek(self, *args):
        return self.f.seek(*args)


class Task:
    """A user-visible queued operation. Workers update the fields; the GUI polls them."""

    def __init__(self, kind, name, fn, *args, **kwargs):
        self.kind, self.name, self.fn, self.args, self.kwargs = kind, name, fn, args, kwargs
        self.state, self.done, self.total, self.cancelled = 'Queued', 0, 0, False

    def check(self):
        if self.cancelled:
            raise Cancelled()

    def __call__(self, s):
        if self.cancelled:
            self.state = 'Cancelled'
            return self
        self.state, self.done = 'Running', 0
        try:
            self.fn(s, *self.args, task=self, **self.kwargs)
            self.state = 'Done'
        except Cancelled:
            self.state = 'Cancelled'
            time.sleep(1)  # give the printer a moment to notice the dropped connection
        except Exception as e:  # noqa: BLE001 - every failure is reported in the queue
            self.state = f'Failed: {e}'
        return self

    @property
    def finished(self):
        return self.state not in ('Queued', 'Running')

    @property
    def status(self):
        if self.state == 'Running' and self.kind == 'Upload' and self.total and self.done >= self.total:
            return 'Finishing...'  # all sent; waiting for the printer to store it
        if self.state == 'Running' and self.total:
            return f'{100 * self.done // self.total}% of {fmt_size(self.total)}'
        if self.state == 'Running' and self.done:
            return fmt_size(self.done)
        return self.state


class Lane(threading.Thread):
    """Executes jobs strictly one at a time over a single keep-alive connection.

    Foreground and background jobs alternate, so a bulk download can't starve thumbnails and
    a pile of thumbnails can't hold up a delete. If `idle` is given, it runs every `interval`
    seconds in between jobs (status polling), backing off while the printer is unreachable.
    """

    def __init__(self, printer, post, idle=None, interval=2.0):
        super().__init__(daemon=True)
        self.printer, self.post, self.idle, self.interval = printer, post, idle, interval
        self.session = printer.session()
        self.jobs = (deque(), deque())  # foreground, background
        self.last = 1
        self.next_idle = 0.0
        self.stopped = False
        self.cv = threading.Condition()
        self.start()

    def submit(self, key, fn, *args, background=False):
        """Run `fn(session, *args)` and post `(key, result_or_exception)` when done."""
        with self.cv:
            self.jobs[background].append((key, fn, args))
            self.cv.notify()

    def poll_now(self):
        with self.cv:
            self.next_idle = 0.0
            self.cv.notify()

    def clear_background(self):
        with self.cv:
            self.jobs[1].clear()

    def stop(self):
        with self.cv:
            self.stopped = True
            self.cv.notify()

    def _take(self):
        with self.cv:
            while not self.stopped:
                if self.idle and time.monotonic() >= self.next_idle:
                    self.next_idle = time.monotonic() + self.interval
                    return self.idle
                for i in (1 - self.last, self.last):
                    if self.jobs[i]:
                        self.last = i
                        return self.jobs[i].popleft()
                self.cv.wait(max(0.0, self.next_idle - time.monotonic()) if self.idle else None)

    def run(self):
        while job := self._take():
            key, fn, args = job
            try:
                result = fn(self.session, *args)
            except Exception as e:  # noqa: BLE001 - handed to the GUI
                result = e
                if job is self.idle:
                    self.next_idle = time.monotonic() + max(self.interval, 5)
            if key and not self.stopped:
                self.post(key, result)
        self.session.close()


class ThumbCache:
    """Thumbnails on disk, keyed by printer + file path + modification time, so an unchanged
    file is never fetched twice. An empty file records "this G-code has no preview"."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key):
        return self.root / hashlib.sha1(key.encode()).hexdigest()

    def get(self, key):
        p = self._path(key)
        return p.read_bytes() if p.exists() else None

    def put(self, key, data):
        tmp = self._path(key).with_suffix('.tmp')
        tmp.write_bytes(data)
        os.replace(tmp, self._path(key))


def fmt_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
