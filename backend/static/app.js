'use strict';

const BACKEND = window.location.origin;
const HISTORY_KEY = 'judge-helper:history-v1';
const HISTORY_LIMIT = 25;
const METADATA_KEY = 'judge-helper:metadata-draft-v1';

// 401 from a protected endpoint = session expired. We DO NOT redirect
// automatically anymore — that would destroy any unsaved recording held in
// page memory. Instead we set a global flag; upload code shows an inline
// "log in" prompt next to the affected queue item.
let sessionExpired = false;
const _origFetch = window.fetch.bind(window);
window.fetch = async function (...args) {
    const resp = await _origFetch(...args);
    if (resp.status === 401) {
        sessionExpired = true;
    }
    return resp;
};

// ---- IndexedDB: persist recorded audio BEFORE any network attempt -----
// If the upload fails (auth, network, server) the blob stays on disk and
// the user can retry. Previously a 401 redirect lost the entire recording.
const IDB_DB = 'judge-helper-recordings';
const IDB_STORE = 'pending';

function openIDB() {
    return new Promise((resolve, reject) => {
        const req = indexedDB.open(IDB_DB, 1);
        req.onupgradeneeded = () => {
            const db = req.result;
            if (!db.objectStoreNames.contains(IDB_STORE)) {
                db.createObjectStore(IDB_STORE, { keyPath: 'id' });
            }
        };
        req.onsuccess = () => resolve(req.result);
        req.onerror = () => reject(req.error);
    });
}

async function saveRecordingToIDB(blob, filename, mimeType, metadata) {
    const id = 'rec_' + Date.now() + '_' + Math.random().toString(36).slice(2, 8);
    const db = await openIDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(IDB_STORE, 'readwrite');
        tx.objectStore(IDB_STORE).put({
            id, blob, filename, mimeType, metadata, createdAt: Date.now(),
        });
        tx.oncomplete = () => { db.close(); resolve(id); };
        tx.onerror = () => { db.close(); reject(tx.error); };
    });
}

async function loadRecordingFromIDB(id) {
    const db = await openIDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(IDB_STORE, 'readonly');
        const req = tx.objectStore(IDB_STORE).get(id);
        req.onsuccess = () => { db.close(); resolve(req.result || null); };
        req.onerror = () => { db.close(); reject(req.error); };
    });
}

async function deleteRecordingFromIDB(id) {
    const db = await openIDB();
    return new Promise((resolve, reject) => {
        const tx = db.transaction(IDB_STORE, 'readwrite');
        tx.objectStore(IDB_STORE).delete(id);
        tx.oncomplete = () => { db.close(); resolve(); };
        tx.onerror = () => { db.close(); reject(tx.error); };
    });
}

async function listPendingRecordings() {
    try {
        const db = await openIDB();
        return await new Promise((resolve, reject) => {
            const tx = db.transaction(IDB_STORE, 'readonly');
            const req = tx.objectStore(IDB_STORE).getAll();
            req.onsuccess = () => { db.close(); resolve(req.result || []); };
            req.onerror = () => { db.close(); reject(req.error); };
        });
    } catch (e) {
        console.warn('IDB list failed', e);
        return [];
    }
}

const $ = (id) => document.getElementById(id);

// =========================================================================
// Card switcher
// =========================================================================
const ALL_CARDS = ['upload-card', 'recording-card', 'queue-card', 'error-card'];
function showCard(id) {
    ALL_CARDS.forEach((c) => { $(c).hidden = (c !== id); });
}

// =========================================================================
// History (localStorage)
// =========================================================================
function loadHistory() {
    try {
        const raw = localStorage.getItem(HISTORY_KEY);
        return raw ? JSON.parse(raw) : [];
    } catch { return []; }
}
function saveHistory(entries) {
    try {
        localStorage.setItem(HISTORY_KEY, JSON.stringify(entries.slice(0, HISTORY_LIMIT)));
    } catch (e) {
        console.warn('localStorage write failed (quota?)', e);
    }
}
function addToHistory(entry) {
    const list = loadHistory().filter((h) => h.id !== entry.id);
    list.unshift(entry);
    saveHistory(list);
    renderHistory();
}
function deleteFromHistory(id) {
    saveHistory(loadHistory().filter((h) => h.id !== id));
    renderHistory();
}
function renderHistory() {}
function makeFilename(entry) {
    const meta = entry.metadata || {};
    const parts = [];
    
    const defendant = (meta.defendant || '').trim().split(' ')[0];
    if (defendant) {
        parts.push(defendant.replace(/[\\/:*?"<>|]/g, '_'));
    } else {
        parts.push('Без_имени');
    }
    
    const dateObj = new Date(entry.timestamp || Date.now());
    const yyyy = dateObj.getFullYear();
    const mm = String(dateObj.getMonth() + 1).padStart(2, '0');
    const dd = String(dateObj.getDate()).padStart(2, '0');
    parts.push(`${yyyy}-${mm}-${dd}`);
    
    parts.push('протокол');
    
    return parts.join('_') + '.docx';
}
function openHistoryEntry(entry) {
    // Inject as a synthetic queue item already in "done" state, so user can
    // edit / download from the usual place.
    const item = {
        key: 'h_' + entry.id,
        file: null,
        filename: entry.filename || 'Из истории',
        sizeMB: '—',
        metadata: entry.metadata || {},
        status: 'done',
        draft: entry.draft,
        duration_min: entry.duration_min,
        model: entry.model,
        jobId: entry.id,
        timestamp: entry.timestamp,
        fromHistory: true,
    };
    // Prepend to queue (so it shows up at top), avoid duplicates
    queue = queue.filter((q) => q.key !== item.key);
    queue.unshift(item);
    showCard('queue-card');
    renderQueue();
}

// =========================================================================
// Queue / state
// =========================================================================
let queue = [];

function getMetadataFromForm() {
    return {};
}

function saveMetadataDraft() {}
function restoreMetadataDraft() {}
['meta-case', 'meta-defendant', 'meta-statute', 'meta-judge'].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener('input', saveMetadataDraft);
});

// Auto-save draft edits back to history (debounced)
const draftSaveTimers = {};
function updateHistoryDraft(jobId, newDraft) {
    if (!jobId) return;
    clearTimeout(draftSaveTimers[jobId]);
    draftSaveTimers[jobId] = setTimeout(() => {
        const list = loadHistory();
        const entry = list.find((h) => h.id === jobId);
        if (entry) {
            entry.draft = newDraft;
            entry.editedAt = new Date().toISOString();
            saveHistory(list);
            renderHistory();
        }
        delete draftSaveTimers[jobId];
    }, 1000);
}

function cleanSurname(name) {
    const baseName = name.substring(0, name.lastIndexOf('.')) || name;
    const match = baseName.trim().match(/^[a-zA-Zа-яА-ЯёЁ]+/);
    if (match) {
        const word = match[0];
        return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
    }
    return baseName;
}

function addFilesToQueue(files, opts = {}) {
    const meta = getMetadataFromForm();
    for (const f of Array.from(files)) {
        const fileMeta = { ...meta };
        if (!fileMeta.defendant) {
            fileMeta.defendant = cleanSurname(f.name);
        }
        const item = {
            key: 'q_' + Math.random().toString(36).slice(2, 10),
            file: f,
            filename: f.name,
            sizeMB: (f.size / 1024 / 1024).toFixed(1),
            metadata: fileMeta,
            idbId: opts.idbId || null,  // set for browser-recorded blobs
            status: 'staged',  // NOT auto-processed — user must click "Расшифровать"
            progress: 0,
        };
        queue.push(item);
    }
    showCard('queue-card');
    renderQueue();
}

function checkQueueScheduler() {
    const active = queue.some((q) => q.status === 'uploading' || q.status === 'processing');
    if (active) return;
    const next = queue.find((q) => q.status === 'queued');
    if (next) {
        processQueueItem(next);
    }
}

async function processQueueItem(item) {
    item.status = 'uploading';
    item.progress = 0;
    renderQueue();
    acquireWakeLock();
    try {
        const jobId = await uploadFile(item);
        item.jobId = jobId;
        item.status = 'processing';
        item.phase = 'uploading_to_aai';
        item.pollStart = Date.now();
        renderQueue();
        pollQueueItem(item);
    } catch (err) {
        if (err && err.code === 'AUTH') {
            // Session expired — DO NOT redirect, blob still in IDB. Show
            // inline prompt with login link + retry button.
            item.status = 'auth_required';
            item.error = err.message;
            renderQueue();
            checkQueueScheduler();
            releaseWakeLockIfDone();
            return;
        }
        item.status = 'error';
        item.error = err.message || String(err);
        renderQueue();
        checkQueueScheduler();
        releaseWakeLockIfDone();
    }
}

function pollQueueItem(item) {
    if (item.pollTimer) clearInterval(item.pollTimer);
    let failures = 0;
    const MAX_FAILURES = 5;
    item.pollTimer = setInterval(async () => {
        try {
            const resp = await fetch(`${BACKEND}/status/${item.jobId}`);
            if (!resp.ok) {
                failures++;
                if (failures >= MAX_FAILURES) {
                    clearInterval(item.pollTimer);
                    item.status = 'error';
                    item.error = `Сервер не отвечает (HTTP ${resp.status})`;
                    renderQueue();
                    checkQueueScheduler();
                    releaseWakeLockIfDone();
                }
                return;
            }
            failures = 0;
            const data = await resp.json();
            if (data.status === 'done') {
                clearInterval(item.pollTimer);
                item.status = 'done';
                item.draft = data.draft;
                item.duration_min = data.duration_min;
                item.model = data.model;
                item.timestamp = new Date().toISOString();
                addToHistory({
                    id: item.jobId,
                    timestamp: item.timestamp,
                    duration_min: item.duration_min,
                    filename: item.filename,
                    metadata: item.metadata,
                    draft: item.draft,
                    model: item.model,
                });
                // Upload succeeded — safe to drop the IDB backup
                if (item.idbId) {
                    deleteRecordingFromIDB(item.idbId).catch((e) => console.warn('IDB delete', e));
                    item.idbId = null;
                }
                renderQueue();
                checkQueueScheduler();
                releaseWakeLockIfDone();
            } else if (data.status === 'transcribed') {
                // Part of a multi-part session — transcribed but not drafted.
                // Stop polling, mark as ready for merging.
                clearInterval(item.pollTimer);
                item.status = 'transcribed';
                item.transcript = data.transcript;
                item.duration_min = data.duration_min;
                renderQueue();
                checkQueueScheduler();
                releaseWakeLockIfDone();
            } else if (data.status === 'error') {
                clearInterval(item.pollTimer);
                item.status = 'error';
                item.error = data.error || 'Неизвестная ошибка';
                renderQueue();
                checkQueueScheduler();
                releaseWakeLockIfDone();
            } else {
                item.phase = data.phase || 'processing';
                // Capture server-side timestamps + AAI audio duration so the
                // render code can show a real ETA. Only set when present so
                // we don't clobber values already received from a prior tick.
                if (data.created_at)         item.created_at = data.created_at;
                if (data.aai_started_at)     item.aai_started_at = data.aai_started_at;
                if (data.drafting_started_at) item.drafting_started_at = data.drafting_started_at;
                if (data.audio_duration_sec) item.audio_duration_sec = data.audio_duration_sec;
                renderQueue();
            }
        } catch (err) {
            failures++;
            if (failures >= MAX_FAILURES) {
                clearInterval(item.pollTimer);
                item.status = 'error';
                item.error = 'Не удалось получить статус.';
                renderQueue();
                checkQueueScheduler();
                releaseWakeLockIfDone();
            }
        }
    }, 5000);
}

function uploadFile(item) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        const form = new FormData();
        form.append('file', item.file);
        for (const [k, v] of Object.entries(item.metadata || {})) {
            if (v) form.append(k, v);
        }
        if (item.markAsPart) {
            form.append('is_part', 'true');
        }
        xhr.timeout = 10 * 60 * 1000;
        xhr.upload.onprogress = (e) => {
            if (e.lengthComputable) {
                item.progress = Math.round(e.loaded / e.total * 100);
                renderQueue();
            }
        };
        xhr.upload.onloadend = () => {
            item.progress = 100;
            item.uploadDone = true;
            renderQueue();
        };
        xhr.onload = () => {
            if (xhr.status === 401) {
                // DO NOT redirect — that would lose the in-memory blob. Caller
                // shows inline auth prompt; recording stays in IndexedDB.
                const err = new Error('Сессия истекла. Войдите снова в новой вкладке и нажмите «Попробовать снова».');
                err.code = 'AUTH';
                reject(err);
                return;
            }
            if (xhr.status >= 200 && xhr.status < 300) {
                try {
                    const data = JSON.parse(xhr.responseText);
                    resolve(data.job_id);
                } catch (e) {
                    reject(new Error('Сервер вернул битый ответ'));
                }
            } else {
                let msg = `HTTP ${xhr.status}`;
                try {
                    const j = JSON.parse(xhr.responseText);
                    if (j.detail) msg += `: ${j.detail}`;
                } catch (_) {}
                reject(new Error(msg));
            }
        };
        xhr.onerror = () => reject(new Error('Нет соединения с сервером. Проверьте интернет.'));
        xhr.ontimeout = () => reject(new Error('Сервер не ответил за 10 минут. Попробуйте файл поменьше.'));
        xhr.onabort = () => reject(new Error('Загрузка прервана'));
        xhr.open('POST', `${BACKEND}/upload`);
        xhr.send(form);
    });
}

// =========================================================================
// Queue rendering
// =========================================================================
function phaseLabel(phase) {
    return {
        'uploading_to_aai': 'Передача файла на расшифровку…',
        'transcribing':     'Расшифровка аудио…',
        'drafting':         'Составление черновика протокола…',
    }[phase] || 'Обработка…';
}

// Estimate transcription progress. Returns { elapsedSec, estimatedSec, pct } or null
// when we don't yet have audio_duration from AssemblyAI.
//
// Empirical multiplier 0.3 = AssemblyAI universal-2 + Russian + diarization runs
// at roughly 30% of audio duration (per user-guide.md "1-5 мин на 10 мин записи").
// Clamp to [60s, 600s] so a 30-second snippet doesn't get a "8s ETA" jitter,
// and a 5-hour recording doesn't promise 90 minutes either.
function estimateTranscribing(item) {
    if (!item.aai_started_at || !item.audio_duration_sec) return null;
    const nowSec = Date.now() / 1000;
    const elapsedSec = Math.max(0, Math.round(nowSec - item.aai_started_at));
    const estimatedSec = Math.max(60, Math.min(600, Math.round(item.audio_duration_sec * 0.3)));
    // Cap at 98% — last 2% reserved for the "done" transition so the bar never
    // sits at 100% with the spinner still spinning (bad UX).
    const pct = Math.min(98, (elapsedSec / estimatedSec) * 100);
    return { elapsedSec, estimatedSec, pct };
}

// Elapsed for the current phase, in seconds. Falls back through:
//   transcribing -> aai_started_at
//   drafting     -> drafting_started_at
//   any          -> created_at
//   (none known) -> pollStart (client-side, lost on reload)
function phaseElapsedSec(item) {
    const nowSec = Date.now() / 1000;
    let startSec = null;
    if (item.phase === 'transcribing' && item.aai_started_at) startSec = item.aai_started_at;
    else if (item.phase === 'drafting' && item.drafting_started_at) startSec = item.drafting_started_at;
    else if (item.created_at) startSec = item.created_at;
    if (startSec != null) return Math.max(0, Math.round(nowSec - startSec));
    return item.pollStart ? Math.round((Date.now() - item.pollStart) / 1000) : 0;
}

function renderQueue() {
    const list = $('queue-list');
    list.innerHTML = '';
    if (queue.length === 0) {
        $('queue-title').textContent = 'Очередь пуста';
    } else {
        const doneCnt = queue.filter((q) => q.status === 'done').length;
        const errCnt  = queue.filter((q) => q.status === 'error').length;
        const stagedCnt = queue.filter((q) => q.status === 'staged').length;
        const inProgCnt = queue.length - doneCnt - errCnt - stagedCnt;
        const parts = [];
        if (stagedCnt) parts.push(`${stagedCnt} в очереди`);
        if (inProgCnt) parts.push(`${inProgCnt} в работе`);
        if (doneCnt)   parts.push(`${doneCnt} готово`);
        if (errCnt)    parts.push(`${errCnt} ошибка`);
        $('queue-title').textContent = parts.length ? `Обработка — ${parts.join(', ')}` : 'Обработка';
    }
    for (const item of queue) {
        list.appendChild(renderQueueItem(item));
    }
    renderStagedBar();
    renderMergeBar();
}

// Bar above the queue when there are staged (not-yet-processed) items.
// Lets mom batch-process them all in one click.
function renderStagedBar() {
    let bar = $('staged-bar');
    if (!bar) {
        bar = document.createElement('div');
        bar.id = 'staged-bar';
        bar.style.cssText = 'padding:1.5rem 0 0; margin-top:1.5rem; border-top:1px solid var(--border); display:flex; align-items:center; gap:1rem; flex-wrap:wrap';
        const list = $('queue-list');
        list.parentNode.insertBefore(bar, list.nextSibling);
    }
    const staged = queue.filter((q) => q.status === 'staged');
    if (staged.length < 1) {
        bar.style.display = 'none';
        return;
    }
    bar.style.display = 'flex';
    bar.innerHTML = '';
    const goBtn = document.createElement('button');
    goBtn.className = 'big';
    goBtn.textContent = staged.length > 1 ? 'Расшифровать всё' : 'Расшифровать';
    goBtn.addEventListener('click', () => {
        staged.forEach((s) => {
            s.status = 'queued';
        });
        renderQueue();
        checkQueueScheduler();
    });
    bar.appendChild(goBtn);
}

// Bar that appears above the list when 2+ items are selected for merging
function renderMergeBar() {
    let bar = $('merge-bar');
    if (!bar) {
        bar = document.createElement('div');
        bar.id = 'merge-bar';
        bar.style.cssText = 'padding:1.5rem 0 0; margin-top:1.5rem; border-top:1px solid var(--border); display:flex; align-items:center; gap:1rem; flex-wrap:wrap';
        const list = $('queue-list');
        list.parentNode.insertBefore(bar, list.nextSibling);
    }
    const selected = queue.filter((q) => q.selected && (q.status === 'done' || q.status === 'transcribed'));
    if (selected.length < 2) {
        bar.style.display = 'none';
        return;
    }
    bar.style.display = 'flex';
    bar.innerHTML = '';
    const label = document.createElement('div');
    label.style.cssText = 'flex:1;min-width:200px';
    label.innerHTML = `<strong>Выбрано ${selected.length}</strong> — объединить как одно заседание?`;
    bar.appendChild(label);
    const mergeBtn = document.createElement('button');
    mergeBtn.textContent = 'Объединить';
    mergeBtn.addEventListener('click', () => mergeSelectedAsSession(selected));
    bar.appendChild(mergeBtn);
    const cancelBtn = document.createElement('button');
    cancelBtn.className = 'secondary';
    cancelBtn.textContent = 'Отмена';
    cancelBtn.addEventListener('click', () => {
        queue.forEach((q) => q.selected = false);
        renderQueue();
    });
    bar.appendChild(cancelBtn);
}

async function mergeSelectedAsSession(selected) {
    const transcriptIds = selected.map((s) => s.jobId).filter(Boolean);
    if (transcriptIds.length < 2) {
        alert('Не удалось определить ID частей. Подождите пока все записи получат transcript_id.');
        return;
    }
    // Deselect originals
    queue.forEach((q) => q.selected = false);

    // Create a new session-item in the queue
    const sessionItem = {
        key: 'ses_' + Math.random().toString(36).slice(2, 10),
        filename: `Заседание из ${transcriptIds.length} частей`,
        sizeMB: '—',
        metadata: selected[0]?.metadata || {},
        status: 'processing',
        phase: 'drafting',
        pollStart: Date.now(),
        isSession: true,
    };
    queue.unshift(sessionItem);
    renderQueue();
    acquireWakeLock();

    try {
        const resp = await fetch(`${BACKEND}/combine-and-draft`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                transcript_ids: transcriptIds,
                metadata: sessionItem.metadata,
            }),
        });
        if (!resp.ok) {
            const errText = await resp.text();
            throw new Error(`HTTP ${resp.status}: ${errText.slice(0, 200)}`);
        }
        const data = await resp.json();
        sessionItem.jobId = data.job_id;
        renderQueue();
        // Poll the session id like a regular item
        pollQueueItem(sessionItem);
    } catch (err) {
        sessionItem.status = 'error';
        sessionItem.error = err.message || String(err);
        renderQueue();
        releaseWakeLockIfDone();
    }
}

function renderQueueItem(item) {
    const wrap = document.createElement('div');
    wrap.className = 'queue-item ' + (
        item.status === 'done' ? 'done' :
        (item.status === 'error' || item.status === 'auth_required') ? 'error' : ''
    );

    const head = document.createElement('div');
    head.className = 'queue-item-head';

    const name = document.createElement('div');
    name.className = 'queue-name';
    name.textContent = (item.isSession ? 'Часть: ' : '') + item.filename;
    head.appendChild(name);
    if (item.sizeMB && item.sizeMB !== '—') {
        const size = document.createElement('span');
        size.className = 'queue-size';
        size.textContent = item.sizeMB + ' МБ';
        head.appendChild(size);
    }
    
    if (item.status === 'staged' || item.status === 'error' || item.status === 'auth_required') {
        const rmBtn = document.createElement('button');
        rmBtn.className = 'queue-remove-btn';
        rmBtn.textContent = '✕';
        rmBtn.title = 'Удалить';
        rmBtn.addEventListener('click', () => {
            if (item.idbId && item.status !== 'done') {
                deleteRecordingFromIDB(item.idbId).catch(() => {});
            }
            queue = queue.filter((q) => q.key !== item.key);
            if (queue.length === 0) showCard('upload-card');
            else renderQueue();
        });
        head.appendChild(rmBtn);
    }
    
    if (item.status === 'done') {
        const dlBtn = document.createElement('button');
        dlBtn.className = 'small';
        dlBtn.textContent = 'Скачать .docx';
        dlBtn.style.cssText = 'margin-left: auto;';
        dlBtn.addEventListener('click', async () => {
            const text = item.expanded
                ? (document.getElementById('draft-' + item.key)?.value || item.draft)
                : item.draft;
            await downloadDocx(text, makeFilename({
                metadata: item.metadata,
                timestamp: item.timestamp,
                filename: item.filename,
            }));
        });
        head.appendChild(dlBtn);
    }
    
    wrap.appendChild(head);

    // Status row
    const status = document.createElement('div');
    status.className = 'queue-status';
    if (item.status === 'uploading') {
        const sp = document.createElement('div'); sp.className = 'spinner'; status.appendChild(sp);
        const t = document.createElement('span');
        t.textContent = item.uploadDone
            ? 'Получение ответа сервера…'
            : `Загрузка файла… ${item.progress}%`;
        status.appendChild(t);
    } else if (item.status === 'processing') {
        const sp = document.createElement('div'); sp.className = 'spinner'; status.appendChild(sp);
        const t = document.createElement('span');
        const elapsed = phaseElapsedSec(item);
        const est = item.phase === 'transcribing' ? estimateTranscribing(item) : null;
        let suffix = elapsed ? ` (${formatHMS(elapsed)}` : '';
        if (est) suffix += ` из ≈${formatHMS(est.estimatedSec)}`;
        suffix += elapsed ? ')' : '';
        t.textContent = phaseLabel(item.phase) + suffix;
        status.appendChild(t);
    } else if (item.status === 'done') {
        const t = document.createElement('span');
        t.className = 'badge-done';
        t.innerHTML = `Готово · ${item.duration_min ?? '—'} мин · ${(item.draft?.length || 0).toLocaleString('ru')} символов`;
        status.appendChild(t);
    } else if (item.status === 'transcribed') {
        const t = document.createElement('span');
        t.className = 'badge-done';
        t.textContent = `Расшифровано · ${item.duration_min ?? '—'} мин · готово к объединению`;
        status.appendChild(t);
    } else if (item.status === 'auth_required') {
        const t = document.createElement('span');
        t.className = 'badge-error';
        t.textContent = 'Войдите заново';
        status.appendChild(t);
    } else if (item.status === 'error') {
        const t = document.createElement('span');
        t.className = 'badge-error';
        t.textContent = 'Ошибка';
        status.appendChild(t);
    } else if (item.status === 'staged') {
        const t = document.createElement('span');
        t.style.color = 'var(--muted)';
        t.textContent = 'Готов к обработке';
        status.appendChild(t);
    } else {
        const t = document.createElement('span');
        t.textContent = 'В очереди…';
        status.appendChild(t);
    }
    wrap.appendChild(status);

    // Progress bar during upload
    if (item.status === 'uploading') {
        const prWrap = document.createElement('div');
        prWrap.className = 'custom-progress';
        const prBar = document.createElement('div');
        prBar.className = 'custom-progress-bar';
        if (item.uploadDone) {
            prBar.classList.add('indeterminate');
        } else {
            prBar.style.width = (item.progress || 0) + '%';
        }
        prWrap.appendChild(prBar);
        wrap.appendChild(prWrap);
    } else if (item.status === 'processing') {
        const prWrap = document.createElement('div');
        prWrap.className = 'custom-progress';
        const prBar = document.createElement('div');
        prBar.className = 'custom-progress-bar';
        
        const est = item.phase === 'transcribing' ? estimateTranscribing(item) : null;
        if (est) {
            prBar.style.width = est.pct + '%';
        } else {
            prBar.classList.add('indeterminate');
        }
        prWrap.appendChild(prBar);
        wrap.appendChild(prWrap);
    }

    if ((item.status === 'error' || item.status === 'auth_required') && item.error) {
        const err = document.createElement('div');
        err.className = 'queue-error-msg';
        err.textContent = item.error;
        wrap.appendChild(err);
    }
    if (item.status === 'auth_required') {
        const hint = document.createElement('div');
        hint.className = 'queue-error-msg';
        hint.style.color = 'var(--text)';
        hint.innerHTML = '<strong>Запись сохранена в браузере</strong> — она не пропадёт. ' +
            'Откройте вход в новой вкладке, войдите, вернитесь сюда и нажмите «Попробовать снова».';
        wrap.appendChild(hint);
    }

    // Actions
    const actions = document.createElement('div');
    actions.className = 'queue-actions';

    if (item.status === 'auth_required') {
        const loginBtn = document.createElement('button');
        loginBtn.className = 'small';
        loginBtn.textContent = 'Войти';
        loginBtn.addEventListener('click', () => {
            // Open login in a separate tab so this page (with the in-memory
            // file/idbId reference) stays alive and ready to retry.
            window.open('/login', '_blank');
        });
        actions.appendChild(loginBtn);

        const retryBtn = document.createElement('button');
        retryBtn.className = 'secondary small';
        retryBtn.textContent = '↻ Попробовать снова';
        retryBtn.addEventListener('click', async () => {
            // If we have the in-memory File, just retry; otherwise reload from IDB.
            if (!item.file && item.idbId) {
                try {
                    const rec = await loadRecordingFromIDB(item.idbId);
                    if (rec) {
                        item.file = new File([rec.blob], rec.filename, { type: rec.mimeType });
                    }
                } catch (e) {
                    item.status = 'error';
                    item.error = 'Не удалось восстановить запись из памяти браузера';
                    renderQueue();
                    return;
                }
            }
            sessionExpired = false;
            item.status = 'queued';
            renderQueue();
            checkQueueScheduler();
        });
        actions.appendChild(retryBtn);
    }

    if (actions.children.length) wrap.appendChild(actions);

    // Expanded view: editable draft + warning
    if (item.expanded && item.status === 'done') {
        const warn = document.createElement('div');
        warn.className = 'warning';
        warn.innerHTML = '<strong>Это черновик.</strong> Правки сохраняются — скачайте .docx чтобы они попали в файл. Сверяйте с аудио: ФИО, даты, статьи УК.';
        wrap.appendChild(warn);

        const ta = document.createElement('textarea');
        ta.id = 'draft-' + item.key;
        ta.spellcheck = false;
        ta.value = item.draft;
        ta.addEventListener('input', () => {
            item.draft = ta.value;
            // Also persist to history so edits survive page reload
            updateHistoryDraft(item.jobId, ta.value);
        });
        wrap.appendChild(ta);
    }

    return wrap;
}

function formatHMS(s) {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return h > 0 ? `${pad(h)}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}

// Update timers periodically while items are in 'processing'.
// Skip while user is editing — otherwise the textarea gets recreated
// on every tick and we lose cursor / selection / scroll position.
setInterval(() => {
    const editing = document.activeElement && (
        document.activeElement.tagName === 'TEXTAREA' ||
        document.activeElement.tagName === 'INPUT'
    );
    if (editing) return;
    if (queue.some((q) => q.status === 'processing')) renderQueue();
}, 1000);

// =========================================================================
// File picker / drag-drop
// =========================================================================
const fileInput = $('file-input');
const dropZone  = $('drop-zone');

dropZone.addEventListener('click', () => fileInput.click());
$('pick-btn').addEventListener('click', () => fileInput.click());
$('add-more-btn').addEventListener('click', () => fileInput.click());

let dragCounter = 0;
document.addEventListener('dragenter', (e) => {
    e.preventDefault();
    dragCounter++;
    $('global-dropzone').classList.add('active');
});
document.addEventListener('dragleave', (e) => {
    e.preventDefault();
    dragCounter--;
    if (dragCounter === 0) $('global-dropzone').classList.remove('active');
});
document.addEventListener('dragover', (e) => { e.preventDefault(); });
document.addEventListener('drop', (e) => {
    e.preventDefault();
    dragCounter = 0;
    $('global-dropzone').classList.remove('active');
    if (e.dataTransfer.files.length) addFilesToQueue(e.dataTransfer.files);
});
fileInput.addEventListener('change', (e) => {
    if (e.target.files.length) {
        addFilesToQueue(e.target.files);
        e.target.value = '';  // allow picking the same file twice
    }
});

// =========================================================================
// Recording (works in both upload-card and queue-card via add-more-record)
// =========================================================================
let mediaRecorder = null;
let recordedChunks = [];
let recordingStart = 0;
let recordingTimer = null;
let recordingStream = null;
let wakeLock = null;
let prevCardBeforeRecording = 'upload-card';

async function startRecording() {
    if (!navigator.mediaDevices || !window.MediaRecorder) {
        showError('Этот браузер не поддерживает запись звука. Попробуйте Safari (iPhone) или Chrome.');
        return;
    }
    try {
        recordingStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
        showError('Не удалось получить доступ к микрофону. Разрешите доступ в настройках.');
        return;
    }
    const candidates = ['audio/webm;codecs=opus', 'audio/webm', 'audio/mp4', 'audio/aac'];
    let mimeType = '';
    for (const t of candidates) {
        if (MediaRecorder.isTypeSupported(t)) { mimeType = t; break; }
    }
    recordedChunks = [];
    try {
        mediaRecorder = mimeType
            ? new MediaRecorder(recordingStream, { mimeType })
            : new MediaRecorder(recordingStream);
    } catch (err) {
        showError('Не удалось запустить запись: ' + err.message);
        recordingStream.getTracks().forEach((t) => t.stop());
        return;
    }
    mediaRecorder.addEventListener('dataavailable', (e) => {
        if (e.data && e.data.size > 0) recordedChunks.push(e.data);
    });
    mediaRecorder.addEventListener('stop', onRecordingStopped);
    mediaRecorder.start(1000);
    recordingStart = Date.now();
    prevCardBeforeRecording = $('queue-card').hidden ? 'upload-card' : 'queue-card';
    showCard('recording-card');
    startTimer();
    acquireWakeLock();
}

$('record-btn').addEventListener('click', startRecording);
$('add-more-record-btn').addEventListener('click', startRecording);
$('stop-rec-btn').addEventListener('click', () => {
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
        mediaRecorder.stop();
    }
});

function startTimer() {
    clearInterval(recordingTimer);
    recordingTimer = setInterval(() => {
        const sec = Math.floor((Date.now() - recordingStart) / 1000);
        $('rec-timer').textContent = formatHMS(sec);
    }, 250);
}

async function onRecordingStopped() {
    clearInterval(recordingTimer);
    if (recordingStream) {
        recordingStream.getTracks().forEach((t) => t.stop());
        recordingStream = null;
    }
    const mimeType = mediaRecorder.mimeType || 'audio/webm';
    const ext = mimeType.includes('mp4') ? 'm4a' : (mimeType.includes('webm') ? 'webm' : 'audio');
    const blob = new Blob(recordedChunks, { type: mimeType });
    const ts = new Date().toISOString().slice(0, 16).replace(/[:T]/g, '-');
    const filename = `zasedanie_${ts}.${ext}`;
    const metadata = getMetadataFromForm();

    // CRITICAL: persist the blob to IndexedDB IMMEDIATELY. Even if the
    // browser crashes, the network drops, or auth fails — the recording is
    // safe on disk and can be recovered next time the page loads.
    let idbId = null;
    try {
        idbId = await saveRecordingToIDB(blob, filename, mimeType, metadata);
    } catch (e) {
        console.error('IDB save failed — recording exists only in memory:', e);
    }

    const file = new File([blob], filename, { type: mimeType });
    addFilesToQueue([file], { idbId });
}

async function acquireWakeLock() {
    if (wakeLock || !('wakeLock' in navigator)) return;
    try {
        wakeLock = await navigator.wakeLock.request('screen');
    } catch (e) {
        console.warn('Wake lock failed:', e);
    }
}
async function releaseWakeLock() {
    if (wakeLock) {
        try { await wakeLock.release(); } catch (e) {}
        wakeLock = null;
    }
}
function releaseWakeLockIfDone() {
    const stillActive = queue.some((q) =>
        q.status === 'uploading' || q.status === 'processing'
    ) || (mediaRecorder && mediaRecorder.state === 'recording');
    if (!stillActive) releaseWakeLock();
}
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' &&
        (mediaRecorder?.state === 'recording' ||
         queue.some((q) => q.status === 'uploading' || q.status === 'processing'))) {
        acquireWakeLock();
    }
});

// =========================================================================
// Queue controls
// =========================================================================
$('clear-queue-btn').addEventListener('click', () => {
    // Stop polling for any in-progress items
    queue.forEach((q) => { if (q.pollTimer) clearInterval(q.pollTimer); });
    queue = [];
    showCard('upload-card');
});

// =========================================================================
// Download helper
// =========================================================================
async function downloadDocx(text, filename) {
    if (!text || !text.trim()) return;
    try {
        const resp = await fetch(`${BACKEND}/render-docx`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text, filename }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        a.remove();
        URL.revokeObjectURL(url);
    } catch (e) {
        // Fallback: plain text
        const txtName = filename.replace(/\.docx$/, '.txt');
        const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = txtName;
        a.click();
        URL.revokeObjectURL(url);
    }
}

// =========================================================================
// Error display
// =========================================================================
function showError(msg) {
    releaseWakeLock();
    showCard('error-card');
    $('error-text').textContent = msg;
}

// =========================================================================
// Changelog (bell icon + popup)
// =========================================================================
const CHANGELOG_KEY = 'judge-helper:changelog-last-seen';

// Add new entries at the TOP. Bump 'version' on each release.
// Date format: 'YYYY-MM-DD' or 'DD месяца YYYY' — kept consistent for readability.
const CHANGELOG = [
    {
        version: 8,
        date: '23 мая 2026',
        items: [
            'Прогресс расшифровки показывает реальное время: «Расшифровка аудио… (1:23 из ≈3:30)» и заполняющаяся полоска. Видно сколько ждать.',
            'Сервер запоминает текущие задачи на диск — если случайно перезапустится (обновление, сбой), черновики и расшифровки не пропадают.',
        ],
    },
    {
        version: 7,
        date: '22 мая 2026',
        items: [
            'История изменений переехала в колокольчик 🔔 справа сверху — не занимает экран. Все прошлые обновления тоже здесь.',
        ],
    },
    {
        version: 6,
        date: '22 мая 2026',
        items: [
            'Запись больше не отправляется автоматически после нажатия «Стоп» — попадает в очередь со статусом «Готов к обработке», вы нажимаете «▶ Расшифровать» когда готовы.',
            'Можно сразу обработать пачку — кнопка «▶ Расшифровать всё» появляется когда 2+ записей ждут.',
            'Заседание с перерывом: загрузите обе части, дождитесь когда обе ✓ готовы, поставьте галочки слева → жмите «🔗 Объединить» → получится один цельный протокол.',
        ],
    },
    {
        version: 5,
        date: '22 мая 2026',
        items: [
            'Добавлено 5 новых образцов разных типов заседаний (особый порядок, апелляция, общий процесс, многоэпизодное дело с приговором, отложение) — помощник точнее угадывает стилистику под тип процесса.',
            'Шрифт черновика — Times New Roman.',
        ],
    },
    {
        version: 4,
        date: '21 мая 2026',
        items: [
            '«ПРОВЕРИТЬ ПЕРЕД СДАЧЕЙ» — короткий чек-лист в конце черновика со списком ключевых фактов (ФИО, статья, сумма, решение). Сверьте только эти строки с аудио — не нужно перечитывать весь протокол.',
            'Шапка протокола заполняется автоматически — помощник слышит в начале заседания номер дела, ФИО подсудимого, статью УК. Поля «Данные дела» нужны только если в записи реквизиты не озвучены.',
        ],
    },
    {
        version: 3,
        date: '20 мая 2026',
        items: [
            'Личный доступ по логину и паролю — посторонние без пароля не зайдут. Браузер запоминает на месяц.',
            'Кнопка «Выйти» в правом верхнем углу.',
        ],
    },
    {
        version: 2,
        date: '20 мая 2026',
        items: [
            'Автосохранение полей «Данные дела» и правок черновика — закрыли страницу случайно, всё на месте.',
            'История последних черновиков сохраняется на устройстве.',
        ],
    },
    {
        version: 1,
        date: '18 мая 2026',
        items: [
            'Запись прямо с телефона — кнопка «🎙 Записать заседание».',
            'Несколько файлов сразу — обрабатываются параллельно.',
            'Правка черновика прямо в браузере перед скачиванием .docx.',
            'Можно установить иконку на главный экран (через «Поделиться» → «На экран Домой»).',
        ],
    },
];

function getLastSeenChangelog() {
    try {
        return parseInt(localStorage.getItem(CHANGELOG_KEY) || '0', 10);
    } catch { return 0; }
}
function setLastSeenChangelog(version) {
    try { localStorage.setItem(CHANGELOG_KEY, String(version)); } catch {}
}
function unreadChangelogCount() {
    const lastSeen = getLastSeenChangelog();
    return CHANGELOG.filter((e) => e.version > lastSeen).length;
}

function updateBellBadge() {
    const count = unreadChangelogCount();
    const badge = $('bell-badge');
    if (count > 0) {
        badge.textContent = count > 9 ? '9+' : String(count);
        badge.hidden = false;
    } else {
        badge.hidden = true;
    }
}

function renderChangelog() {
    const list = $('changelog-list');
    list.innerHTML = '';
    const lastSeen = getLastSeenChangelog();
    for (const entry of CHANGELOG) {
        const div = document.createElement('div');
        div.className = 'changelog-entry' + (entry.version > lastSeen ? ' unread' : '');
        const head = document.createElement('div');
        head.className = 'changelog-entry-head';
        const ver = document.createElement('span');
        ver.className = 'changelog-version';
        ver.textContent = `Версия ${entry.version}`;
        head.appendChild(ver);
        const date = document.createElement('span');
        date.className = 'changelog-date';
        date.textContent = entry.date;
        head.appendChild(date);
        div.appendChild(head);
        const ul = document.createElement('ul');
        ul.className = 'changelog-items';
        for (const txt of entry.items) {
            const li = document.createElement('li');
            li.textContent = txt;
            ul.appendChild(li);
        }
        div.appendChild(ul);
        list.appendChild(div);
    }
}

function openChangelog() {
    renderChangelog();
    $('changelog-popup').hidden = false;
    // Mark as read — set lastSeen to the highest version
    const latest = Math.max(...CHANGELOG.map((e) => e.version));
    setLastSeenChangelog(latest);
    updateBellBadge();
}
function closeChangelog() {
    $('changelog-popup').hidden = true;
}

$('bell-btn').addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    if ($('changelog-popup').hidden) openChangelog();
    else closeChangelog();
});
$('changelog-close-btn').addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    closeChangelog();
});
// Click outside the popup closes it
document.addEventListener('click', (e) => {
    const popup = $('changelog-popup');
    if (popup.hidden) return;
    const bell = $('bell-btn');
    if (!popup.contains(e.target) && !bell.contains(e.target) && e.target !== bell) {
        closeChangelog();
    }
});

// =========================================================================
// Recover pending recordings from IndexedDB (failed/orphaned uploads)
// =========================================================================
async function recoverPendingRecordings() {
    const pending = await listPendingRecordings();
    if (!pending.length) return;
    for (const rec of pending) {
        const file = new File([rec.blob], rec.filename, { type: rec.mimeType });
        const sizeMB = (file.size / 1024 / 1024).toFixed(1);
        const item = {
            key: 'q_' + Math.random().toString(36).slice(2, 10),
            file,
            filename: rec.filename,
            sizeMB,
            metadata: rec.metadata || {},
            idbId: rec.id,
            status: 'auth_required',  // gets the recovery UI with retry button
            error: 'Запись осталась с прошлого раза. Войдите если нужно — и нажмите «Попробовать снова».',
            progress: 0,
            recovered: true,
        };
        queue.push(item);
    }
    showCard('queue-card');
    renderQueue();
}

// =========================================================================
// Init
// =========================================================================
updateBellBadge();
renderChangelog();  // pre-populate so popup is always ready
restoreMetadataDraft();
renderHistory();
recoverPendingRecordings();
if ('serviceWorker' in navigator) {
    window.addEventListener('load', () => {
        navigator.serviceWorker.register('/sw.js').catch((err) => {
            console.warn('SW registration failed:', err);
        });
    });
}