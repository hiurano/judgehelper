"""Exercise the actual ASGI receive stream, including early rejection."""
import asyncio
from pathlib import Path

import pytest
from starlette.requests import ClientDisconnect

from backend import auth, main
from backend.db import JobStore, UserStore


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    store = JobStore(str(tmp_path / 'jobs.db'))
    users = UserStore(str(tmp_path / 'users.db'))
    users.create_user('u', 'test-password')
    monkeypatch.setattr(auth, 'user_store', users)
    monkeypatch.setattr(main, 'user_store', users)
    monkeypatch.setattr(main, 'jobs', store)
    monkeypatch.setattr(main, 'UPLOAD_DIR', tmp_path / 'uploads')
    monkeypatch.setattr(main, 'ASSEMBLYAI_KEY', 'fake')
    monkeypatch.setattr(main, 'MAX_UPLOAD_BYTES', 1024 * 1024)
    monkeypatch.setattr(main, '_free_disk_bytes', lambda: 10 * 1024**3)
    monkeypatch.setattr(main, 'spawn', lambda coro, **kwargs: coro.close())
    return store


def request(parts, *, extra_headers=(), path="/upload", method="POST"):
    async def run():
        iterator = iter(parts)
        output = []

        async def receive():
            item = next(iterator)
            if item is None:
                return {'type': 'http.disconnect'}
            return {'type': 'http.request', 'body': item[0], 'more_body': item[1]}

        async def send(message):
            output.append(message)

        scope = {
            'type': 'http', 'asgi': {'version': '3.0'}, 'http_version': '1.1',
            'method': method, 'scheme': 'http', 'path': path,
            'raw_path': path.encode(), 'query_string': b'', 'root_path': '',
            'server': ('test', 80), 'client': ('127.0.0.1', 1234),
            'headers': [
                (b'content-type', b'multipart/form-data; boundary=review'),
                (b'cookie', f'{auth.SESSION_COOKIE}={auth.make_session_token("u")}'.encode()),
                *extra_headers,
            ],
        }
        await main.app(scope, receive, send)
        return next(m['status'] for m in output if m['type'] == 'http.response.start')
    return asyncio.run(run())


HEADER = b'--review\r\nContent-Disposition: form-data; name="file"; filename="hearing.wav"\r\n\r\n'
END = b'\r\n--review--\r\n'


def test_declared_oversize_rejected_without_consuming_body(uploads):
    assert request([], extra_headers=[(b'content-length', b'9999999999')]) == 413
    assert len(uploads) == 0


def test_active_limit_rejected_without_consuming_body(uploads, monkeypatch):
    monkeypatch.setattr(main, 'MAX_ACTIVE_JOBS_PER_USER', 1)
    uploads['busy'] = {'status': 'processing', 'user_id': 'u'}
    assert request([]) == 429
    assert len(uploads) == 1


def test_recording_cannot_be_uploaded_under_a_different_account(uploads):
    assert request([], extra_headers=[(b'x-recording-owner', b'other-account')]) == 403
    assert len(uploads) == 0


def test_chunked_oversize_cleans_file_and_releases_slot(uploads):
    block = b'x' * (512 * 1024)
    assert request([(HEADER + b'RIFF', True), (block, True), (block, True)]) == 413
    assert len(uploads) == 0
    assert list(main.UPLOAD_DIR.iterdir()) == []


@pytest.mark.parametrize('tail', [[None], [(b'', False)]])
def test_interrupted_multipart_cleans_partial_audio(uploads, tail):
    parts = [(HEADER + b'RIFF' + b'x' * 100, True), *tail]
    if tail == [None]:
        with pytest.raises(ClientDisconnect):
            request(parts)
    else:
        assert request(parts) == 400
    assert len(uploads) == 0
    assert list(main.UPLOAD_DIR.iterdir()) == []


def test_disk_reserve_checked_while_receiving(uploads, monkeypatch):
    calls = iter([10 * 1024**3, 10 * 1024**3, main.DISK_RESERVE_BYTES])
    monkeypatch.setattr(main, '_free_disk_bytes', lambda: next(calls))
    assert request([(HEADER + b'RIFF' + b'x' * 100, True), (b'x' * 100, True)]) == 507
    assert len(uploads) == 0
    assert list(main.UPLOAD_DIR.iterdir()) == []


def test_audio_larger_than_docker_tmpfs_never_spools_to_tmp(uploads, monkeypatch):
    import starlette.formparsers as forms
    original = forms.SpooledTemporaryFile

    def spool(*args, **kwargs):
        handle = original(*args, **kwargs)
        def forbidden():
            pytest.fail('Audio was spooled into the default temporary directory')
        handle.rollover = forbidden
        return handle

    monkeypatch.setattr(forms, 'SpooledTemporaryFile', spool)
    monkeypatch.setattr(main, 'MAX_UPLOAD_BYTES', 140 * 1024**2)
    block = b'x' * 1024**2

    def parts():
        yield HEADER + b'RIFF', True
        for _ in range(129):
            yield block, True
        yield END, False

    assert request(parts()) == 200
    paths = list(main.UPLOAD_DIR.iterdir())
    assert len(paths) == 1
    assert paths[0].stat().st_size == 129 * 1024**2 + 4
    assert uploads.get_pending_jobs()[0]['phase'] == 'uploading_to_aai'
    assert uploads.get_pending_jobs()[0]['filename'] == 'hearing.wav'
    assert Path(paths[0]).read_bytes()[:4] == b'RIFF'


def test_large_multipart_header_is_rejected_before_accumulating_body(uploads):
    header = b'--review\r\nContent-Disposition: form-data; name="file"; filename="'
    assert request([(header, True), (b'x' * 9000, True)]) == 400
    assert len(uploads) == 0
    assert list(main.UPLOAD_DIR.iterdir()) == []


def test_reserved_upload_consumes_one_slot_and_cannot_be_replayed(uploads, monkeypatch):
    monkeypatch.setattr(main, 'MAX_ACTIVE_JOBS_PER_USER', 1)
    assert request([], path='/uploads') == 200
    job_id = uploads.get_pending_jobs()[0]['id']
    headers = [(b'x-upload-id', job_id.encode())]
    assert request([(HEADER + b'RIFF' + b'x' * 100 + END, False)], extra_headers=headers) == 200
    assert len(uploads) == 1
    assert request([], extra_headers=headers) == 404


def test_cancelled_reservation_cannot_be_revived_by_late_upload(uploads):
    job_id, _ = main.reserve_upload('u', 'awaiting_upload')
    assert request([], path=f'/jobs/{job_id}', method='DELETE') == 200
    assert request([], extra_headers=[(b'x-upload-id', job_id.encode())]) == 404
    assert len(uploads) == 0


def test_foreign_upload_reservation_is_not_consumed(uploads):
    job_id, _ = main.reserve_upload('other', 'awaiting_upload')
    assert request([], extra_headers=[(b'x-upload-id', job_id.encode())]) == 404
    assert uploads.get(job_id)['phase'] == 'awaiting_upload'


def test_cancel_during_receiving_removes_partial_file(uploads):
    job_id, _ = main.reserve_upload('u', 'awaiting_upload')
    def parts():
        yield HEADER + b'RIFF' + b'x' * 100, True
        assert uploads.delete(job_id)
        yield b'x' * 100, True
    assert request(parts(), extra_headers=[(b'x-upload-id', job_id.encode())]) == 404
    assert list(main.UPLOAD_DIR.iterdir()) == []
    assert len(uploads) == 0
