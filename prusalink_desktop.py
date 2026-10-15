"""PrusaLink Desktop: a PySimpleGUI client for the PrusaLink web API on Prusa printers."""
import base64
import datetime as dt
import io
import os
import queue
import re
import sys
import time
from collections import deque
from pathlib import Path

import PySimpleGUI as sg
from PIL import Image, ImageDraw

from prusalink import Lane, Printer, Task, ThumbCache, fmt_size

APP = 'PrusaLink Desktop'
ICON, PREVIEW = (64, 36), (256, 144)  # file-list thumbnail box, large preview box
HISTORY = 300  # temperature samples kept for the graph
ORANGE, BLUE, MUTED, GRID = '#fa6831', '#3b9fe0', '#9a9a9a', '#3a3a3a'
OPERATIONAL = {'IDLE', 'READY', 'FINISHED', 'STOPPED'}
SORTS = {'Name': ('name', False), 'Name (Z-A)': ('name', True), 'Newest first': ('m_timestamp', True),
         'Oldest first': ('m_timestamp', False), 'Largest first': ('size', True)}
TELEMETRY = ['Status', 'Nozzle', 'Heatbed', 'Print speed', 'Flow', 'Z height', 'Fans', 'Nozzle Ø']
JOB_PROPS = ['Remaining', 'Estimated end', 'Printing time', 'File size']
GCODE_TYPES = (('G-code', '*.gcode *.bgcode *.gco *.g'), ('All files', '*.*'))


# --- small helpers -----------------------------------------------------------------------------
def png(im):
    buf = io.BytesIO()
    im.save(buf, 'PNG')
    return base64.b64encode(buf.getvalue())


def fit(data, box, progress=None):
    """Letterbox image bytes into `box`; with `progress`, grey out the part not printed yet."""
    canvas = Image.new('RGBA', box)
    try:
        im = Image.open(io.BytesIO(data)).convert('RGBA')
    except Exception:  # noqa: BLE001 - no/unreadable thumbnail: empty box
        return png(canvas)
    im.thumbnail(box)
    if progress is not None:
        grey = im.convert('L').point(lambda v: v // 2).convert('RGBA')
        grey.putalpha(im.getchannel('A'))
        cut = round(im.height * (1 - progress / 100))
        grey.paste(im.crop((0, cut, im.width, im.height)), (0, cut))
        im = grey
    canvas.paste(im, ((box[0] - im.width) // 2, (box[1] - im.height) // 2))
    return png(canvas)


def draw_icon(shapes):
    im = Image.new('RGBA', ICON)
    d = ImageDraw.Draw(im)
    for xy, fill in shapes:
        d.rounded_rectangle(xy, 3, fill=fill)
    return png(im)


FOLDER = draw_icon([((16, 6, 32, 14), '#c98a1e'), ((16, 10, 48, 31), '#f0b03c')])
NO_PREVIEW = draw_icon([((24, 4, 40, 32), '#5a5a5a'), ((27, 9, 37, 11), '#808080'), ((27, 14, 37, 16), '#808080')])
LOADING = png(Image.new('RGBA', ICON))


def fmt_duration(s):
    if s is None:
        return '-'
    if s < 60:
        return 'less than a minute'
    h, m = divmod(int(s) // 60, 60)
    return f'{h} h {m} min' if h else f'{m} min'


def fmt_end(s):
    if s is None:
        return '-'
    end, today = dt.datetime.now() + dt.timedelta(seconds=s), dt.date.today()
    day = {today: 'Today', today + dt.timedelta(1): 'Tomorrow'}.get(end.date(), f'{end:%a %d %b}')
    return f'{day} at {end:%H:%M}'


def fmt_date(ts):
    return f'{dt.datetime.fromtimestamp(ts):%Y-%m-%d %H:%M}' if ts else ''


def safe_filename(name):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip() or 'unnamed.gcode'


def cache_dir():
    if sys.platform == 'win32':
        base = os.environ.get('LOCALAPPDATA', '~')
    elif sys.platform == 'darwin':
        base = '~/Library/Caches'
    else:
        base = os.environ.get('XDG_CACHE_HOME', '~/.cache')
    return Path(base).expanduser() / 'PrusaLink-Desktop' / 'thumbnails'


def ask_conflicts(names, where):
    """'Overwrite', 'Skip' or None (cancel) for files that already exist."""
    if not names:
        return 'Overwrite'
    shown = '\n'.join(names[:10]) + (f'\n... and {len(names) - 10} more' if len(names) > 10 else '')
    return sg.popup(f'{len(names)} file(s) already exist {where}:\n\n{shown}', title=APP,
                    custom_text=('Overwrite', 'Skip'))


def configure():
    """Connection settings dialog. Returns True when saved."""
    get = sg.user_settings_get_entry
    label = dict(size=15)
    layout = [[sg.Text('Printer address', **label), sg.Input(get('host', ''), key='host', size=30)],
              [sg.Text('Username', **label), sg.Input(get('username', 'maker'), key='username', size=30)],
              [sg.Text('Password', **label), sg.Input(get('password', ''), key='password', size=30,
                                                      password_char='*')],
              [sg.Text('The password is shown on the printer under Settings > Network > PrusaLink.\n'
                       'Leave the username empty to send it as an API key instead of a Digest login.',
                       text_color=MUTED)],
              [sg.Push(), sg.Button('Test'), sg.Button('Save', bind_return_key=True), sg.Button('Cancel')]]
    win = sg.Window('Connection', layout, modal=True)
    while True:
        event, values = win.read()
        if event in (sg.WIN_CLOSED, 'Cancel'):
            win.close()
            return False
        if event == 'Test':
            printer = Printer(values['host'], values['username'], values['password'])
            try:
                with printer.session() as s:
                    printer.request(s, 'GET', '/api/v1/status', timeout=(3, 5))
                sg.popup('Connected.', title='Connection')
            except Exception as e:  # noqa: BLE001
                sg.popup_error(f'Connection failed:\n{e}', title='Connection')
        if event == 'Save':
            for k in ('host', 'username', 'password'):
                sg.user_settings_set_entry(k, values[k].strip())
            win.close()
            return True


# --- layout --------------------------------------------------------------------------------------
def make_layout():
    def prop(label, key):
        return [sg.Text(label, size=13, text_color=MUTED), sg.Text('-', key=key, size=24, font='Helvetica 10 bold')]

    telemetry = [prop(label, f'tel:{label}') for label in TELEMETRY]
    job = [[sg.Image(key='-JOBIMG-', size=PREVIEW), sg.Column([
        [sg.Text('No active job', key='-JOBNAME-', font='Helvetica 12 bold', size=36)],
        [sg.ProgressBar(100, size=(26, 12), key='-JOBBAR-', bar_color=(ORANGE, GRID)),
         sg.Text('', key='-JOBPCT-', size=7)],
        *[prop(label, f'job:{label}') for label in JOB_PROPS],
        [sg.Button('Pause', key='pause'), sg.Button('Resume', key='resume'), sg.Button('Stop', key='stop')],
    ])]]
    dashboard = [[sg.Column(telemetry, vertical_alignment='top', pad=(10, 10)), sg.Column([
        [sg.Graph((560, 160), (0, 0), (HISTORY, 300), key='-GRAPH-', background_color='#1e1e1e')],
        [sg.Frame('Job', job, expand_x=True)],
        [sg.Frame('Upload', [[sg.Button('Upload G-code...', key='upload_root'),
                              sg.Checkbox('Start print after upload', key='-PRINTAFTER-')]], expand_x=True)],
        [sg.Text('', key='-TRANSFER-', text_color=MUTED)],
    ], vertical_alignment='top')]]

    tree = sg.Tree(sg.TreeData(), headings=['Size', 'Modified'], col0_heading='Name', col0_width=34,
                   col_widths=[9, 15], auto_size_columns=False, num_rows=8, row_height=ICON[1] + 4,
                   select_mode=sg.TABLE_SELECT_MODE_EXTENDED, enable_events=True, key='files',
                   justification='left', expand_x=True, expand_y=True)
    storage = [
        [sg.Combo([], key='storage', readonly=True, enable_events=True, size=10),
         sg.Text('', key='-PATH-', expand_x=True), sg.Text('Sort'),
         sg.Combo(list(SORTS), 'Name', key='sort', readonly=True, enable_events=True, size=13)],
        [tree, sg.Column([[sg.Image(key='-PREVIEW-', size=PREVIEW)],
                          [sg.Text('', key='-DETAILS-', size=(34, 9))]], vertical_alignment='top')],
        [sg.Button('Up', key='up'), sg.Button('Refresh', key='refresh'), sg.Button('New folder...', key='mkdir'),
         sg.Button('Upload...', key='upload'), sg.VerticalSeparator(), sg.Button('Print', key='print'),
         sg.Button('Download...', key='download'), sg.Button('Delete', key='delete'), sg.Push(),
         sg.Text('', key='-SELINFO-', text_color=MUTED)],
        [sg.Frame('Queue', [
            [sg.Table([], ['Operation', 'File', 'Status'], key='queue', col_widths=[9, 44, 26],
                      auto_size_columns=False, num_rows=5, justification='left', expand_x=True,
                      select_mode=sg.TABLE_SELECT_MODE_EXTENDED)],
            [sg.Button('Cancel selected', key='cancel'), sg.Button('Retry failed', key='retry'),
             sg.Button('Clear finished', key='clear')]], expand_x=True)],
    ]
    return [
        [sg.Text('', key='-NAME-', font='Helvetica 14 bold'), sg.Text('', key='-STATE-', font='Helvetica 14', text_color=ORANGE),
         sg.Push(), sg.Button('Connection...', key='connection')],
        [sg.TabGroup([[sg.Tab('Dashboard', dashboard, key='-DASHBOARD-'), sg.Tab('Storage', storage, key='-STORAGE-')]],
                     key='tab', enable_events=True, expand_x=True, expand_y=True)],
        [sg.Text('Not connected', key='-STATUSBAR-', text_color=MUTED, expand_x=True)],
    ]


# --- application ---------------------------------------------------------------------------------
class App:
    def __init__(self):
        self.window = sg.Window(APP, make_layout(), resizable=True, finalize=True,
                                enable_close_attempted_event=True)
        self.window['files'].bind('<Double-Button-1>', '+open')
        self.window['files'].bind('<Return>', '+open')
        self.window['files'].bind('<Delete>', '+delete')
        self.cache = ThumbCache(cache_dir())
        self.icons = {}  # thumbnail cache key -> rendered list icon
        self.tasks, self.queue_rows = [], None
        self.lanes, self.values, self.last_tick = (), {}, 0.0
        # Workers report back through this queue, never by touching Tk: PySimpleGUI's
        # write_event_value() calls into Tk from the worker thread, which can crash Tk builds.
        self.inbox = queue.SimpleQueue()
        self.connect()

    # Connection --------------------------------------------------------------------------------
    def connect(self):
        self.shutdown()
        while not self.inbox.empty():  # drop results still in flight from the previous connection
            self.inbox.get()
        get = sg.user_settings_get_entry
        self.printer = Printer(get('host'), get('username', 'maker'), get('password', ''))
        # Two lanes, each one request at a time: `control` polls status and runs quick commands,
        # `transfer` works through the queue (and thumbnails) without blocking the dashboard.
        self.control = Lane(self.printer, self.post, idle=('status', self.printer.status, ()),
                            interval=float(get('poll_interval', 2)))
        self.transfer = Lane(self.printer, self.post)
        self.lanes = (self.control, self.transfer)
        self.info, self.state, self.temps = {}, None, deque(maxlen=HISTORY)
        self.job_id = self.job_status = self.job_file = self.job_thumb = None
        self.storages, self.root, self.cwd, self.entries, self.selected = [], '', [], {}, []
        self.etag, self.listing_pending, self.last_listing, self.details_for = None, False, 0.0, None
        self.thumbs_pending = set()
        self.window['-NAME-'].update(self.printer.base)
        self.control.submit('info', self.printer.info)
        self.control.submit('storages', self.printer.storages)

    def post(self, key, value):  # called from worker threads
        self.inbox.put((key, value))

    def shutdown(self):
        self.cancel(self.tasks)
        for lane in self.lanes:
            lane.stop()
        if self.lanes:
            self.transfer.join(timeout=3)  # let a running transfer clean up its partial file

    def run(self):
        while self.step():
            pass
        self.shutdown()
        self.window.close()

    def step(self, timeout=100):
        """Handle one GUI event plus whatever the workers reported; False once the user has quit."""
        event, values = self.window.read(timeout=timeout)
        if event in (sg.WIN_CLOSED, sg.WINDOW_CLOSE_ATTEMPTED_EVENT):
            pending = sum(not t.finished for t in self.tasks)
            return not (event == sg.WIN_CLOSED or not pending or sg.popup_yes_no(
                f'{pending} queued operation(s) have not finished. Quit anyway?', title=APP) == 'Yes')
        self.values = values
        self.dispatch(event, values.get(event))
        while not self.inbox.empty():
            self.dispatch(*self.inbox.get())
        if time.monotonic() - self.last_tick > 0.5:
            self.last_tick = time.monotonic()
            self.tick()
        return True

    def dispatch(self, event, value):
        handler = getattr(self, 'on_' + str(event).replace('+', '_'), None)
        if handler:
            handler(value)

    def statusbar(self, text):
        self.window['-STATUSBAR-'].update(text)

    def failed(self, what, error):
        if isinstance(error, Exception):
            self.statusbar(f'{what} failed: {error}')
            return True
        return False

    # Dashboard ---------------------------------------------------------------------------------
    def on_info(self, info):
        if not self.failed('Reading printer info', info):
            self.info = info or {}
            self.window['-NAME-'].update(self.info.get('name') or self.info.get('hostname') or self.printer.base)

    def on_status(self, st):
        if isinstance(st, Exception):
            self.state = 'OFFLINE'
            self.window['-STATE-'].update('Offline')
            self.statusbar(f'Offline: {st}')
            return self.update_buttons()
        p, w, previous = st.get('printer', {}), self.window, self.state
        self.state = p.get('state', 'UNKNOWN').upper()
        if previous in (None, 'OFFLINE'):
            self.statusbar(f'Connected to {self.printer.base}')
        if previous == 'OFFLINE':  # we may have started without the printer: fetch what we missed
            if not self.info:
                self.control.submit('info', self.printer.info)
            if not self.storages:
                self.control.submit('storages', self.printer.storages)
        name = self.window['-NAME-'].get()
        w['-STATE-'].update(self.state.title())
        w.set_title(f'{name} - {self.state.title()} - {APP}')

        def temp(key):
            current, target = p.get(f'temp_{key}'), p.get(f'target_{key}') or 0
            return '-' if current is None else f'{current:.1f} / {target:.0f} °C'
        nozzle_d = self.info.get('nozzle_diameter')
        for label, value in zip(TELEMETRY, (
                self.state.title(), temp('nozzle'), temp('bed'),
                f"{p['speed']} %" if 'speed' in p else '-', f"{p['flow']} %" if 'flow' in p else '-',
                f"{p['axis_z']:.2f} mm" if 'axis_z' in p else '-',
                f"{p.get('fan_hotend', '-')} / {p.get('fan_print', '-')} rpm",
                f'{nozzle_d:.2f} mm' if nozzle_d else '-')):
            w[f'tel:{label}'].update(value)
        self.temps.append((p.get('temp_nozzle'), p.get('temp_bed')))
        self.draw_graph()

        job = st.get('job')
        if (job or {}).get('id') != self.job_id:
            self.job_id, self.job_file, self.job_thumb = (job or {}).get('id'), None, None
            if job:
                self.control.submit('job', self.printer.job)
        self.job_status = job
        self.update_job()
        transfer = st.get('transfer')
        w['-TRANSFER-'].update(f"Printer is receiving a file: {transfer.get('progress', 0):.0f}%" if transfer else '')
        self.update_buttons()

    def draw_graph(self):
        g = self.window['-GRAPH-']
        g.erase()
        for t in range(50, 300, 50):
            g.draw_line((0, t), (HISTORY, t), color=GRID)
            g.draw_text(f'{t}°', (4, t + 9), color=MUTED, font='Helvetica 8', text_location=sg.TEXT_LOCATION_LEFT)
        for i, (label, color) in enumerate((('Nozzle', ORANGE), ('Heatbed', BLUE))):
            offset = HISTORY - len(self.temps)
            points = [(offset + x, v[i]) for x, v in enumerate(self.temps) if v[i] is not None]
            if len(points) > 1:
                g.draw_lines(points, color=color, width=2)
            g.draw_text(label, (HISTORY - 4, 288 - 22 * i), color=color, font='Helvetica 9 bold',
                        text_location=sg.TEXT_LOCATION_RIGHT)

    def on_job(self, job):
        if self.failed('Reading job', job) or not job or job.get('id') != self.job_id:
            return
        self.job_file = job.get('file') or {}
        ref, ts = self.job_file.get('refs', {}).get('thumbnail'), self.job_file.get('m_timestamp')
        if ref:
            key = self.thumb_key(ref, ts)
            self.job_thumb = self.cache.get(key)
            if self.job_thumb is None:  # one small request: worth jumping the transfer queue for
                self.control.submit('thumb', self.fetch_thumb, key, ref)
        self.update_job()

    def update_job(self):
        w, js, file = self.window, self.job_status, self.job_file or {}
        if not js:
            w['-JOBNAME-'].update('No active job')
            w['-JOBIMG-'].update(data=LOADING)
            w['-JOBBAR-'].update(0)
            w['-JOBPCT-'].update('')
            for label in JOB_PROPS:
                w[f'job:{label}'].update('-')
            return
        progress = js.get('progress') or 0
        remaining = js.get('time_remaining') if self.state in ('PRINTING', 'PAUSED') else None
        w['-JOBNAME-'].update(file.get('display_name') or file.get('name') or 'Loading...')
        w['-JOBIMG-'].update(data=fit(self.job_thumb or b'', PREVIEW, progress))
        w['-JOBBAR-'].update(progress)
        w['-JOBPCT-'].update(f'{progress:.0f} %')
        for label, value in zip(JOB_PROPS, (fmt_duration(remaining), fmt_end(remaining),
                                            fmt_duration(js.get('time_printing')),
                                            fmt_size(file['size']) if file.get('size') else '-')):
            w[f'job:{label}'].update(value)

    def update_buttons(self):
        w, state = self.window, self.state
        one = self.entries.get(self.selected[0]) if len(self.selected) == 1 else None
        w['pause'].update(disabled=state != 'PRINTING')
        w['resume'].update(disabled=state != 'PAUSED')
        w['stop'].update(disabled=state not in ('PRINTING', 'PAUSED', 'ATTENTION'))
        w['print'].update(disabled=not (state in OPERATIONAL and one and one['type'] != 'FOLDER'))
        w['download'].update(disabled=not any(self.entries[k]['type'] != 'FOLDER' for k in self.selected))
        w['delete'].update(disabled=not self.selected)
        w['up'].update(disabled=not self.cwd)
        w['-PRINTAFTER-'].update(disabled=state not in OPERATIONAL)
        if state not in OPERATIONAL:
            w['-PRINTAFTER-'].update(False)

    def command(self, fn, *args):
        self.control.submit('command', fn, *args)

    def on_command(self, result):
        if isinstance(result, Exception):
            sg.popup_error(str(result), title=APP)
        self.control.poll_now()

    def on_pause(self, _):
        self.command(self.printer.pause, self.job_id)

    def on_resume(self, _):
        self.command(self.printer.resume, self.job_id)

    def on_stop(self, _):
        if sg.popup_yes_no('Stop the current print?', title=APP) == 'Yes':
            self.command(self.printer.stop, self.job_id)

    def on_upload_root(self, _):
        storage = next((s for s in self.storages if not s.get('read_only')), None)
        src = sg.popup_get_file('Upload', no_window=True, file_types=GCODE_TYPES) if storage else None
        if src:
            dest = f'{self.storage_root(storage)}/{Path(src).name}'
            self.control.submit('upload_check', self.check_upload, dest, src, self.values['-PRINTAFTER-'])

    def check_upload(self, s, dest, src, print_after):
        return self.printer.exists(s, dest), dest, src, print_after

    def on_upload_check(self, result):
        if self.failed('Upload', result):
            return
        exists, dest, src, print_after = result
        if not exists or ask_conflicts([Path(src).name], 'on the printer') == 'Overwrite':
            self.enqueue(Task('Upload', Path(src).name, self.printer.upload, dest, src, overwrite=exists,
                              print_after=print_after))

    # Storage -----------------------------------------------------------------------------------
    @staticmethod
    def storage_root(storage):
        return '/' + storage['path'].strip('/')

    def folder(self):
        return self.root + ''.join(f'/{name}' for name, _ in self.cwd)

    def thumb_key(self, ref, timestamp):
        return f'{self.printer.base}|{ref}|{timestamp}'

    def on_storages(self, storages):
        if self.failed('Reading storage', storages):
            return
        self.storages = [s for s in storages if s.get('available', True)] or storages
        names = [s.get('name') or s['path'].strip('/') for s in self.storages]
        self.window['storage'].update(values=names, value=names[0] if names else '')
        if self.storages:
            self.root = self.storage_root(self.storages[0])
            self.list_dir(force=True)

    def on_storage(self, name):
        names = [s.get('name') or s['path'].strip('/') for s in self.storages]
        if name in names:
            self.root, self.cwd = self.storage_root(self.storages[names.index(name)]), []
            self.list_dir(force=True)

    def list_dir(self, force=False):
        if force:
            self.etag, self.entries, self.selected, self.details_for = None, {}, [], None
            self.transfer.clear_background()  # thumbnails for the folder we're leaving
            self.thumbs_pending.clear()
            self.show_files()
        elif self.listing_pending:
            return
        self.listing_pending, self.last_listing = True, time.monotonic()
        self.control.submit('listing', self.fetch_listing, self.folder(), self.etag)

    def fetch_listing(self, s, path, etag):
        return path, self.printer.listdir(s, path, etag)

    def on_listing(self, result):
        self.listing_pending = False
        if self.failed('Listing files', result) or result[0] != self.folder() or result[1] is None:
            return  # error, user moved on, or unchanged (304)
        children, self.etag = result[1]
        folder = self.folder()
        self.entries = {}
        for e in children:
            e.setdefault('display_name', e['name'])
            e['path'] = f"{folder}/{e['name']}"
            if e.get('refs', {}).get('thumbnail'):
                e['thumb_key'] = self.thumb_key(e['refs']['thumbnail'], e.get('m_timestamp'))
            self.entries[e['path']] = e
        self.selected = [k for k in self.selected if k in self.entries]
        missing = []
        for e in self.sorted_entries():
            key = e.get('thumb_key')
            if key and key not in self.icons and key not in self.thumbs_pending:
                data = self.cache.get(key)
                if data is None:
                    missing.append(e)
                else:
                    self.icons[key] = fit(data, ICON) if data else NO_PREVIEW
        self.show_files()
        for e in missing:  # fetched in display order, interleaved with queued transfers
            self.thumbs_pending.add(e['thumb_key'])
            self.transfer.submit('thumb', self.fetch_thumb, e['thumb_key'], e['refs']['thumbnail'], background=True)

    def sorted_entries(self):
        field, reverse = SORTS[self.values.get('sort') or 'Name']
        def key(e):
            return e['display_name'].lower() if field == 'name' else e.get(field) or 0
        files = sorted(self.entries.values(), key=key, reverse=reverse)
        return [e for e in files if e['type'] == 'FOLDER'] + [e for e in files if e['type'] != 'FOLDER']

    def show_files(self):
        data = sg.TreeData()
        for e in self.sorted_entries():
            icon = FOLDER if e['type'] == 'FOLDER' else self.icons.get(e.get('thumb_key'), LOADING if
                                                                        e.get('thumb_key') else NO_PREVIEW)
            data.insert('', e['path'], ' ' + e['display_name'],
                        [fmt_size(e['size']) if e.get('size') else '', fmt_date(e.get('m_timestamp'))], icon=icon)
        tree = self.window['files']
        tree.update(values=data)
        storage = self.window['storage'].get()
        self.window['-PATH-'].update('/'.join([storage] + [display for _, display in self.cwd]) + '/')
        if self.selected:
            tree.update(select_node_keys=self.selected)  # fires a 'files' event, which updates the preview
        else:
            self.on_files([])

    def fetch_thumb(self, s, key, ref):
        try:
            data = self.printer.thumbnail(s, ref)
        except Exception:  # noqa: BLE001 - not cached, so it's retried when the folder is listed again
            return key, None
        self.cache.put(key, data)
        return key, data

    def on_thumb(self, result):
        key, data = result
        self.thumbs_pending.discard(key)
        if data is None:
            return
        self.icons[key] = fit(data, ICON) if data else NO_PREVIEW
        tree = self.window['files']
        for path, e in self.entries.items():
            if e.get('thumb_key') == key and path in tree.KeyToID:
                tree.update(key=path, icon=self.icons[key])
        if self.selected == [p for p, e in self.entries.items() if e.get('thumb_key') == key][:1]:
            self.window['-PREVIEW-'].update(data=fit(data, PREVIEW))
        if self.job_file and key == self.thumb_key(self.job_file.get('refs', {}).get('thumbnail'),
                                                   self.job_file.get('m_timestamp')):
            self.job_thumb = data
            self.update_job()

    def on_files(self, keys):
        self.selected = [k for k in keys or [] if k in self.entries]
        one = self.entries[self.selected[0]] if len(self.selected) == 1 else None
        data = self.cache.get(one['thumb_key']) if one and one.get('thumb_key') else None
        self.window['-PREVIEW-'].update(data=FOLDER if one and one['type'] == 'FOLDER' else fit(data or b'', PREVIEW))
        self.show_details(one)
        if one and one['type'] != 'FOLDER' and one['path'] != self.details_for:
            self.details_for = one['path']
            self.control.submit('details', self.fetch_details, one['path'])
        n = len(self.selected)
        self.window['-SELINFO-'].update(f'{n} selected' if n > 1 else '')
        self.update_buttons()

    def fetch_details(self, s, path):
        return path, self.printer.details(s, path)

    def on_details(self, result):
        if not isinstance(result, Exception) and result[0] in self.entries and self.selected == [result[0]]:
            e = self.entries[result[0]]
            e.update({k: v for k, v in (result[1] or {}).items() if k in ('size', 'meta')})
            if e.get('size'):  # Buddy firmware leaves sizes out of folder listings
                self.window['files'].update(key=e['path'], value=[fmt_size(e['size']), fmt_date(e.get('m_timestamp'))])
            self.show_details(e)

    def show_details(self, e):
        if not e:
            return self.window['-DETAILS-'].update('')
        meta = e.get('meta') or {}
        lines = [e['display_name'], '']
        for label, value in (('Size', fmt_size(e['size']) if e.get('size') else None),
                             ('Modified', fmt_date(e.get('m_timestamp'))),
                             ('Print time', fmt_duration(meta['estimated_print_time'])
                              if meta.get('estimated_print_time') else None),
                             ('Material', meta.get('filament_type')),
                             ('Layer height', f"{meta['layer_height']} mm" if meta.get('layer_height') else None),
                             ('Nozzle', f"{meta['nozzle_diameter']} mm" if meta.get('nozzle_diameter') else None),
                             ('Filament', f"{meta['filament used [g]']:.1f} g" if meta.get('filament used [g]')
                              else None)):
            if value:
                lines.append(f'{label}: {value}')
        self.window['-DETAILS-'].update('\n'.join(lines))

    def on_files_open(self, keys):
        e = self.entries.get((keys or [None])[0])
        if e and e['type'] == 'FOLDER':
            self.cwd.append((e['name'], e['display_name']))
            self.list_dir(force=True)
        elif e:
            self.on_print(None)

    def on_up(self, _):
        if self.cwd:
            self.cwd.pop()
            self.list_dir(force=True)

    def on_refresh(self, _):
        self.list_dir(force=True)

    def on_sort(self, _):
        self.show_files()

    def on_tab(self, tab):
        if tab == '-STORAGE-':
            self.list_dir()

    def tick(self):
        rows = [[t.kind, t.name, t.status] for t in self.tasks]
        if rows != self.queue_rows:
            self.window['queue'].update(values=rows, select_rows=self.values.get('queue') or [])
            self.queue_rows = rows
            running = [t for t in self.tasks if t.state == 'Running']
            queued = sum(t.state == 'Queued' for t in self.tasks)
            if running or queued:
                current = f'{running[0].kind} {running[0].name} ({running[0].status})' if running else ''
                self.statusbar(f'{current}  {queued} more queued' if queued else current)
            elif self.tasks:
                failed = sum(t.state.startswith('Failed') for t in self.tasks)
                self.statusbar(f'Queue finished: {len(self.tasks) - failed} done' + (f', {failed} failed' if failed else ''))
        # Keep the listing fresh while it's on screen; unchanged folders cost a 304.
        if self.values.get('tab') == '-STORAGE-' and time.monotonic() - self.last_listing > 10:
            self.list_dir()

    def on_print(self, _):
        e = self.entries.get(self.selected[0]) if len(self.selected) == 1 else None
        if e and e['type'] != 'FOLDER' and self.state in OPERATIONAL and \
                sg.popup_ok_cancel(f"Start printing {e['display_name']}?", title=APP) == 'OK':
            self.command(self.printer.start_print, e['path'])
            self.window['-DASHBOARD-'].select()

    def on_mkdir(self, _):
        name = sg.popup_get_text('Folder name', title=APP)
        if name and name.strip():
            self.control.submit('mkdir_done', self.printer.mkdir, f'{self.folder()}/{name.strip()}')

    def on_mkdir_done(self, result):
        if not self.failed('Creating folder', result):
            self.list_dir()

    def on_download(self, _):
        files = [self.entries[k] for k in self.selected
                 if self.entries[k]['type'] != 'FOLDER' and self.entries[k].get('refs', {}).get('download')]
        if not files:
            return
        folder = sg.popup_get_folder('Download to', no_window=True, initial_folder=sg.user_settings_get_entry(
            'download_dir', str(Path.home() / 'Downloads')))
        if not folder:
            return
        sg.user_settings_set_entry('download_dir', folder)
        targets = [(e, Path(folder) / safe_filename(e['display_name'])) for e in files]
        choice = ask_conflicts([t.name for _, t in targets if t.exists()], 'in the download folder')
        for e, target in targets:
            if choice and not (choice == 'Skip' and target.exists()):
                task = Task('Download', e['display_name'], self.printer.download, e['refs']['download'], str(target))
                task.total = e.get('size') or 0
                self.enqueue(task)

    def on_delete(self, _):
        items = [self.entries[k] for k in self.selected]
        if not items:
            return
        folders = sum(e['type'] == 'FOLDER' for e in items)
        what = f'{len(items) - folders} file(s)' + (f' and {folders} folder(s) with everything inside them'
                                                     if folders else '')
        shown = '\n'.join(e['display_name'] for e in items[:10]) + ('\n...' if len(items) > 10 else '')
        if sg.popup_ok_cancel(f'Delete {what} from the printer?\n\n{shown}', title=APP) == 'OK':
            for e in items:
                self.enqueue(Task('Delete', e['display_name'], self.printer.delete, e['path']))

    on_files_delete = on_delete

    def on_upload(self, _):
        sources = sg.popup_get_file('Upload', no_window=True, multiple_files=True, file_types=GCODE_TYPES)
        if not sources:
            return
        sources = sources.split(';') if isinstance(sources, str) else sources
        existing = {e['display_name'].lower() for e in self.entries.values()}
        clashes = [Path(src).name for src in sources if Path(src).name.lower() in existing]
        choice = ask_conflicts(clashes, f'in {self.window["-PATH-"].get()}')
        for src in sources:
            name = Path(src).name
            clash = name in clashes
            if choice and not (choice == 'Skip' and clash):
                self.enqueue(Task('Upload', name, self.printer.upload, f'{self.folder()}/{name}', src, overwrite=clash))

    # Queue -------------------------------------------------------------------------------------
    def enqueue(self, task):
        self.tasks.append(task)
        self.transfer.submit('task', task)

    def on_task(self, task):
        if isinstance(task, Exception):
            return
        if task.state.startswith('Failed'):
            self.statusbar(f'{task.kind} {task.name}: {task.state}')
        if all(t.finished for t in self.tasks):
            self.list_dir()

    @staticmethod
    def cancel(tasks):
        for task in tasks:
            task.cancelled = True
            if task.state == 'Queued':  # otherwise it would say "Queued" until the worker reaches it
                task.state = 'Cancelled'

    def on_cancel(self, _):
        self.cancel(self.tasks[i] for i in self.values.get('queue') or [] if i < len(self.tasks))

    def on_retry(self, _):
        for i, task in enumerate(self.tasks):
            if task.state.startswith('Failed'):
                self.tasks[i] = Task(task.kind, task.name, task.fn, *task.args, **task.kwargs)
                self.transfer.submit('task', self.tasks[i])

    def on_clear(self, _):
        self.tasks = [t for t in self.tasks if not t.finished]

    def on_connection(self, _):
        if configure():
            self.connect()


def main():
    sg.theme('DarkGrey13')
    sg.set_options(font='Helvetica 10')
    sg.user_settings_filename('PrusaLink-Desktop.json')
    if sg.user_settings_get_entry('host') or configure():
        App().run()


if __name__ == '__main__':
    main()
