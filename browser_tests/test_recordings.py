"""Real-browser regressions for persistent audio and cross-tab ownership.

Install requirements-browser.txt and run `playwright install chromium`, then
`pytest browser_tests`. The browser uses fake audio; all HTTP APIs are mocked.
"""
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import sync_playwright


STATIC = Path(__file__).resolve().parents[1] / 'backend' / 'static'


@pytest.fixture
def ui():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(args=[
            '--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream',
        ])
        context = browser.new_context(permissions=['microphone'])
        state = {'owner': 'account-a', 'upload_status': 200, 'uploads': 0, 'reservations': 0, 'jobs': {}}

        def respond(route):
            path = urlsplit(route.request.url).path
            if path == '/api/me':
                route.fulfill(json={'username': 'alice', 'recording_owner': state['owner'], 'max_active_jobs': 3})
            elif path == '/jobs':
                route.fulfill(json={'jobs': list(state['jobs'].values())})
            elif path == '/uploads':
                state['reservations'] += 1
                job_id = f"reserved-{state['reservations']}"
                state['jobs'][job_id] = {'id': job_id, 'status': 'processing', 'phase': 'awaiting_upload'}
                route.fulfill(json={'job_id': job_id})
            elif path.startswith('/jobs/') and route.request.method == 'DELETE':
                state['jobs'].pop(path.rsplit('/', 1)[-1], None)
                route.fulfill(json={'ok': True})
            elif path == '/upload':
                state['uploads'] += 1
                if state['upload_status'] != 200:
                    route.fulfill(status=state['upload_status'], json={'detail': 'test upload failure'})
                else:
                    job_id = route.request.headers["x-upload-id"]
                    state['jobs'][job_id] = {'id': job_id, 'status': 'processing', 'phase': 'transcribing'}
                    route.fulfill(json={'job_id': job_id})
            elif path.startswith('/status/'):
                job = state['jobs'].get(path.rsplit('/', 1)[-1])
                route.fulfill(status=200 if job else 404, json=job or {})
            else:
                file = STATIC / ('index.html' if path == '/' else path.removeprefix('/static/'))
                if file.is_file():
                    route.fulfill(path=str(file))
                else:
                    route.fulfill(status=404, body='')

        context.route('**/*', respond)
        errors = []
        context.on('page', lambda page: page.on('pageerror', lambda error: errors.append(str(error))))
        yield context, state
        browser.close()
        assert not errors, errors


def open_page(context):
    page = context.new_page()
    page.goto('http://localhost/')
    page.evaluate('recordingsReady')
    return page


def record_audio(page):
    page.click('#record-btn')
    page.wait_for_function('isRecording && audioChunks.length > 0')
    page.click('#stop-record-btn')
    page.wait_for_function('!isRecording && queue.some(q => q.recordingId)')


def saved_count(page):
    return page.evaluate('''() => recordingStore.transaction('readonly', (records, chunks, result) => {
        const request = records.count(); request.onsuccess = () => result(request.result);
    })''')


def test_stop_reload_failed_upload_and_completion(ui):
    context, state = ui
    page = open_page(context)
    record_audio(page)
    size = page.evaluate('queue[0].file.size')
    assert size > 0
    assert saved_count(page) == 1
    page.reload()
    page.evaluate('recordingsReady')
    assert page.evaluate('queue[0].file.size') == size
    assert saved_count(page) == 1

    state['upload_status'] = 503
    page.evaluate('processQueueItem(queue[0])')
    assert page.evaluate('queue[0].status') == 'error'
    page.reload()
    page.evaluate('recordingsReady')
    assert page.evaluate('queue[0].file.size') == size

    state['upload_status'] = 200
    page.evaluate('processQueueItem(queue[0])')
    job_id = page.evaluate('queue[0].jobId')
    uploads = state['uploads']
    assert saved_count(page) == 1
    page.reload()
    page.evaluate('recordingsReady')
    assert page.evaluate('queue.filter(q => q.jobId === ' + json.dumps(job_id) + ').length') == 1
    assert state['uploads'] == uploads
    assert saved_count(page) == 1

    state['jobs'][job_id].update(status='done', draft='Готовый протокол')
    page.evaluate('queue.find(q => q.jobId).forceCheck()')
    assert saved_count(page) == 0


def test_two_tabs_do_not_take_or_delete_each_others_recording(ui):
    context, _ = ui
    first = open_page(context)
    first.click('#record-btn')
    first.wait_for_function('isRecording && audioChunks.length > 0')
    second = open_page(context)
    assert second.evaluate('queue.length') == 0
    record_audio(second)
    assert saved_count(second) == 2
    first.evaluate('stopRecording()')
    first.wait_for_function('!isRecording && queue.length === 1')
    assert first.evaluate('queue.length') == 1
    assert second.evaluate('queue.length') == 1
    first.close()
    second.reload()
    second.evaluate('recordingsReady')
    assert second.evaluate('queue.length') == 2
    assert saved_count(second) == 2


def test_account_isolation_and_unowned_legacy_chunks(ui):
    context, state = ui
    first = open_page(context)
    record_audio(first)
    first.evaluate('''() => new Promise((resolve, reject) => {
        const request = indexedDB.open('DictaphoneDB', 1);
        request.onupgradeneeded = () => request.result.createObjectStore('chunks', { autoIncrement: true });
        request.onsuccess = () => {
            const db = request.result;
            const tx = db.transaction('chunks', 'readwrite');
            tx.objectStore('chunks').add(new Blob(['legacy']));
            tx.oncomplete = () => { db.close(); resolve(); };
            tx.onerror = () => reject(tx.error);
        };
    })''')
    first.close()
    state['owner'] = 'account-b'
    second = open_page(context)
    assert second.evaluate('queue.length') == 0
    assert saved_count(second) == 0
    assert second.evaluate('''() => new Promise(resolve => {
        const request = indexedDB.open('DictaphoneDB', 1);
        request.onsuccess = () => {
            const count = request.result.transaction('chunks').objectStore('chunks').count();
            count.onsuccess = () => { request.result.close(); resolve(count.result); };
        };
    })''') == 1
    second.close()
    state['owner'] = 'account-a'
    first = open_page(context)
    assert first.evaluate('queue.length') == 1
    assert saved_count(first) == 1


def test_storage_failure_stops_recording_and_offers_audio_download(ui):
    context, _ = ui
    page = open_page(context)
    page.click('#record-btn')
    page.wait_for_function('isRecording && audioChunks.length > 0')
    page.evaluate("() => { recordingStore.append = async () => { throw new Error('QuotaExceededError'); }; }")
    page.wait_for_function('!isRecording && queue.some(q => q.recordingId)')
    assert page.evaluate('queue[0].file.size') > 0
    assert saved_count(page) == 1
    with page.expect_download() as download:
        page.get_by_text('Скачать аудио', exact=True).click()
    assert download.value.suggested_filename.endswith('.webm')


def test_explicit_deletion_only_removes_selected_local_recording(ui):
    context, _ = ui
    page = open_page(context)
    record_audio(page)
    page.evaluate("showCard('upload-card')")
    record_audio(page)
    assert saved_count(page) == 2
    retained_id = page.evaluate('queue[1].recordingId')
    page.on('dialog', lambda dialog: dialog.accept())
    page.locator('.queue-remove-btn').first.click()
    page.wait_for_function('queue.length === 1')
    assert saved_count(page) == 1
    assert page.evaluate('queue[0].recordingId') == retained_id
    page.reload()
    page.evaluate('recordingsReady')
    assert page.evaluate('queue[0].recordingId') == retained_id


def test_closed_tab_recovers_committed_chunks_of_unfinished_recording(ui):
    context, _ = ui
    first = open_page(context)
    first.click('#record-btn')
    first.wait_for_function('isRecording && audioChunks.length > 0')
    # Wait for a transaction queued after the chunk write to finish.
    first.evaluate("recordingStore.transaction('readonly', (records, chunks, result) => result(true))")
    first.close()
    second = open_page(context)
    assert second.evaluate('queue.length') == 1
    assert second.evaluate('queue[0].file.size') > 0
    assert saved_count(second) == 1


def stage_audio(page):
    page.evaluate("addFilesToQueue([new File(['RIFFaudio'], 'test.wav', {type: 'audio/wav'})])")


def test_status_retry_after_outage_does_not_upload_again(ui):
    context, state = ui
    state['jobs']['existing'] = {'id': 'existing', 'status': 'processing', 'phase': 'transcribing'}
    page = open_page(context)
    page.wait_for_function('queue.some(q => q.jobId === "existing")')
    page.route('**/status/*', lambda route: route.fulfill(status=503, json={}))
    page.evaluate('''async () => {
        const item = queue.find(q => q.jobId === 'existing');
        for (let i = 0; i < 30; i++) await item.forceCheck();
    }''')
    assert page.evaluate('queue[0].status') == 'error'
    assert page.evaluate('!!queue[0].file') is False
    page.unroute('**/status/*')
    state['jobs']['existing'].update(status='done', draft='recovered protocol')
    page.get_by_text('↻ Проверить статус снова', exact=True).click()
    page.wait_for_function('queue[0].status === "done"')
    assert state['uploads'] == state['reservations'] == 0
    assert page.evaluate('queue[0].jobId') == 'existing'


def test_server_failure_without_local_file_requests_source_file(ui):
    context, state = ui
    state['jobs']['existing'] = {'id': 'existing', 'status': 'processing', 'phase': 'transcribing'}
    page = open_page(context)
    page.wait_for_function('queue.length === 1')
    state['jobs']['existing'].update(status='error', error='failed')
    page.evaluate('queue[0].forceCheck()')
    assert page.get_by_text('↻ Повторить обработку', exact=True).is_disabled()
    assert state['uploads'] == 0


def test_cancel_aborts_xhr_and_deletes_reservation(ui):
    context, state = ui
    page = open_page(context)
    page.on('dialog', lambda dialog: dialog.accept())
    pending = []
    page.route('**/upload', lambda route: pending.append(route))
    stage_audio(page)
    page.evaluate('void processQueueItem(queue[0])')
    page.wait_for_function('queue[0].xhr !== undefined && queue[0].jobId')
    page.evaluate('void cancelQueueItem(queue[0])')
    page.wait_for_function('queue.length === 0')
    assert state['jobs'] == {}
    assert state['uploads'] == 0


def test_cancel_waits_for_late_reservation_response_without_sending_audio(ui):
    context, state = ui
    page = open_page(context)
    page.on('dialog', lambda dialog: dialog.accept())
    pending = []
    page.route('**/uploads', lambda route: pending.append(route))
    stage_audio(page)
    page.evaluate('void processQueueItem(queue[0])')
    page.wait_for_timeout(100)
    assert len(pending) == 1
    page.evaluate('void cancelQueueItem(queue[0])')
    state['jobs']['late-id'] = {'id': 'late-id', 'status': 'processing', 'phase': 'awaiting_upload'}
    pending[0].fulfill(json={'job_id': 'late-id'})
    page.wait_for_function('queue.length === 0')
    assert state['jobs'] == {}
    assert state['uploads'] == 0


def test_cancelled_job_does_not_return_from_stale_history_response(ui):
    context, state = ui
    state['jobs']['existing'] = {'id': 'existing', 'status': 'processing', 'phase': 'transcribing'}
    page = open_page(context)
    page.wait_for_function('queue.length === 1')
    page.on('dialog', lambda dialog: dialog.accept())
    # A delayed list from before deletion must not put the cancelled job back.
    page.route('**/jobs', lambda route: route.fulfill(json={'jobs': [
        {'id': 'existing', 'status': 'processing', 'phase': 'transcribing'},
    ]}))
    page.evaluate('cancelQueueItem(queue[0])')
    page.evaluate('loadHistoryJobs()')
    assert page.evaluate('queue.length') == 0
