"""Tests for the API client, worker lanes and thumbnail cache, run against tools/mock_printer.py."""
import queue
import sys
import threading
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).parents[1]), str(Path(__file__).parents[1] / 'tools')]
import mock_printer  # noqa: E402
from prusalink import ApiError, Lane, Printer, Task, ThumbCache  # noqa: E402


@pytest.fixture(scope='module')
def server(tmp_path_factory):
    folder = tmp_path_factory.mktemp('usb')
    mock_printer.make_samples(folder)
    server = mock_printer.make_server(['--port', '0', '--dir', str(folder), '--latency', '0', '--rate', '1e9'])
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


@pytest.fixture
def usb(server):
    return mock_printer.args.dir


@pytest.fixture
def printer(server):
    return Printer(f'127.0.0.1:{server.server_port}', 'maker', 'secret')


@pytest.mark.parametrize('username, password, ok', [('maker', 'secret', True), ('', 'secret', True),
                                                    ('maker', 'wrong', False), ('', '', False)])
def test_auth(printer, username, password, ok):
    p = Printer(printer.base, username, password)
    with p.session() as s:
        if ok:
            assert p.status(s)['printer']['state']
        else:
            with pytest.raises(ApiError, match='401'):
                p.status(s)


def test_listing_short_names_and_etag(printer):
    with printer.session() as s:
        children, etag = printer.listdir(s, '/usb')
        benchy = next(c for c in children if c['display_name'].startswith('Benchy'))
        assert benchy['name'] == 'BENCHY~1.GCO' and benchy['refs']['download'] == '/usb/BENCHY~1.GCO'
        assert printer.listdir(s, '/usb', etag) is None  # 304 Not Modified
        assert printer.details(s, '/usb/BENCHY~1.GCO')['size'] > 0
        assert printer.thumbnail(s, benchy['refs']['thumbnail']).startswith(b'\x89PNG')
        assert printer.thumbnail(s, '/thumb/l/usb/NOTHUM~1.GCO') == b''


def test_download_upload_delete(printer, usb, tmp_path):
    with printer.session() as s:
        dest = tmp_path / 'clip.gcode'
        task = Task('Download', 'clip', printer.download, '/usb/CLIP~1.GCO', str(dest))
        assert task(s).state == 'Done' and dest.read_bytes() == (usb / 'clip.gcode').read_bytes()

        upload = Task('Upload', 'copy', printer.upload, '/usb/Copy of clip.gcode', str(dest))
        assert upload(s).state == 'Done' and (usb / 'Copy of clip.gcode').stat().st_size == dest.stat().st_size
        again = Task('Upload', 'copy', printer.upload, '/usb/Copy of clip.gcode', str(dest))
        assert again(s).state == 'Failed: 409 Conflict - File already exists'
        forced = Task('Upload', 'copy', printer.upload, '/usb/Copy of clip.gcode', str(dest), overwrite=True)
        assert forced(s).state == 'Done'

        assert Task('Delete', 'copy', printer.delete, '/usb/Copy of clip.gcode')(s).state == 'Done'
        assert not (usb / 'Copy of clip.gcode').exists()
        assert Task('Delete', 'copy', printer.delete, '/usb/Copy of clip.gcode')(s).state.startswith('Failed: 404')


def test_upload_with_fresh_digest_session(printer, tmp_path):
    """The first request of a Digest session gets a 401; that must not be the upload itself."""
    src = tmp_path / 'fresh.gcode'
    src.write_bytes(b'G28\n' * 100_000)
    with printer.session() as s:
        assert Task('Upload', 'fresh', printer.upload, '/usb/fresh.gcode', str(src))(s).state == 'Done'
        printer.delete(s, '/usb/fresh.gcode')


def test_cancelled_download_leaves_nothing(printer, tmp_path, monkeypatch):
    monkeypatch.setattr(mock_printer.args, 'rate', 200_000)
    task = Task('Download', 'big', printer.download, '/usb/PHONES~1.GCO', str(tmp_path / 'big.gcode'))
    threading.Timer(0.5, lambda: setattr(task, 'cancelled', True)).start()
    with printer.session() as s:
        assert task(s).state == 'Cancelled'
    assert 0 < task.done < task.total and not list(tmp_path.iterdir())


def test_lane_is_sequential_and_alternates(printer):
    gate, results = threading.Event(), queue.SimpleQueue()
    mock_printer.max_in_flight = 0
    lane = Lane(printer, lambda key, value: results.put((key, value)))
    lane.submit('gate', lambda s: gate.wait(5))  # hold the lane while everything else is queued
    for i in range(4):
        lane.submit(f'thumb{i}', printer.thumbnail, '/thumb/l/usb/CLIP~1.GCO', background=True)
    for i in range(2):
        lane.submit(f'task{i}', printer.status)
    gate.set()
    order = [results.get(timeout=10)[0] for _ in range(7)]
    lane.stop()
    assert order == ['gate', 'thumb0', 'task0', 'thumb1', 'task1', 'thumb2', 'thumb3']
    assert mock_printer.max_in_flight == 1


def test_thumb_cache(tmp_path):
    cache = ThumbCache(tmp_path / 'thumbs')
    assert cache.get('a') is None
    cache.put('a', b'png')
    cache.put('b', b'')  # "no preview" is remembered too
    assert ThumbCache(tmp_path / 'thumbs').get('a') == b'png' and cache.get('b') == b''
