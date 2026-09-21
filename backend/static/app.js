'use strict';

const BACKEND = window.location.origin;

const $ = (id) => document.getElementById(id);

// =========================================================================
// Card switcher
// =========================================================================
const ALL_CARDS = ['upload-card', 'queue-card'];
function showCard(id) {
    ALL_CARDS.forEach((c) => { 
        const el = $(c);
        if (el) el.hidden = (c !== id); 
    });
}

// =========================================================================
// Helpers
// =========================================================================
function cleanSurname(name) {
    const baseName = name.substring(0, name.lastIndexOf('.')) || name;
    const match = baseName.trim().match(/^[a-zA-Zа-яА-ЯёЁ]+/);
    if (match) {
        const word = match[0];
        return word.charAt(0).toUpperCase() + word.slice(1).toLowerCase();
    }
    return baseName;
}

function makeFilename(entry) {
    let name = '';
    if (entry.filename) {
        name = entry.filename.replace(/\.[^/.]+$/, '').trim();
    } else if (entry.metadata && entry.metadata.defendant) {
        name = entry.metadata.defendant.trim();
    }
    if (!name) name = 'Протокол';
    name = name.charAt(0).toUpperCase() + name.slice(1);
    name = name.replace(/[\\/:*?"<>|]/g, '_');
    return name + '.docx';
}

function formatHMS(s) {
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return h > 0 ? `${pad(h)}:${pad(m)}:${pad(sec)}` : `${pad(m)}:${pad(sec)}`;
}

// WakeLock
let wakeLock = null;
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
    );
    if (!stillActive) releaseWakeLock();
}

// =========================================================================
// Queue State & Processing
// =========================================================================
let queue = [];

function addFilesToQueue(files) {
    for (const f of Array.from(files)) {
        const fileMeta = { defendant: cleanSurname(f.name) };
        const item = {
            key: 'q_' + Math.random().toString(36).slice(2, 10),
            file: f,
            filename: f.name,
            sizeMB: (f.size / 1024 / 1024).toFixed(1),
            metadata: fileMeta,
            status: 'staged',
            progress: 0,
        };
        queue.push(item);
    }
    showCard('queue-card');
    renderQueue();
}

// Mirrors MAX_ACTIVE_JOBS_PER_USER on the server; refreshed from /api/me.
let maxActiveJobs = 3;
let retryQueuedTimer = null;

function checkQueueScheduler() {
    const uploadingCount = queue.filter((q) => q.status === 'uploading').length;
    if (uploadingCount >= 2) return;
    // The server counts everything it is still working on, not just uploads.
    // Starting a fourth one only earns a 429 and a failed-looking item.
    const activeCount = queue.filter(
        (q) => q.status === 'uploading' || q.status === 'processing'
    ).length;
    if (activeCount >= maxActiveJobs) return;
    const next = queue.find((q) => q.status === 'queued');
    if (next) {
        processQueueItem(next);
    }
}

// Safety net for a slot freed by something this tab cannot see: another
// device, or a job that finished while we were offline.
function scheduleQueuedRetry() {
    if (retryQueuedTimer) return;
    retryQueuedTimer = setTimeout(() => {
        retryQueuedTimer = null;
        if (queue.some((q) => q.status === 'queued')) {
            checkQueueScheduler();
            scheduleQueuedRetry();
        }
    }, 15000);
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
        checkQueueScheduler();
        pollQueueItem(item);
    } catch (err) {
        if (err && err.code === 'LIMIT') {
            // Server is already at its per-user limit: hold this one back
            // instead of showing the operator a failure they cannot act on.
            item.status = 'queued';
            item.progress = 0;
            renderQueue();
            scheduleQueuedRetry();
            releaseWakeLockIfDone();
            return;
        }
        if (err && err.code === 'AUTH') {
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
    const MAX_FAILURES = 30; // 30 * 5s = 2.5 minutes grace period for cold-starts/network blips

    const checkStatus = async () => {
        if (item.status === 'done' || item.status === 'error' || item.status === 'auth_required') {
            if (item.pollTimer) clearInterval(item.pollTimer);
            return;
        }
        try {
            const resp = await fetch(`${BACKEND}/status/${item.jobId}`);
            if (!resp.ok) {
                failures++;
                if (failures >= MAX_FAILURES) {
                    if (item.pollTimer) clearInterval(item.pollTimer);
                    item.status = 'error';
                    item.error = `Сервер временно недоступен (HTTP ${resp.status}). Нажмите кнопку ниже для повторной проверки.`;
                    renderQueue();
                    checkQueueScheduler();
                    releaseWakeLockIfDone();
                } else if (failures > 2) {
                    item.reconnecting = true;
                    renderQueue();
                }
                return;
            }
            failures = 0;
            if (item.reconnecting) {
                item.reconnecting = false;
            }
            const data = await resp.json();
            if (data.status === 'done') {
                if (item.pollTimer) clearInterval(item.pollTimer);
                item.status = 'done';
                item.phase = 'done';
                item.draft = data.draft;
                item.duration_min = data.duration_min;
                item.speakers_count = data.speakers_count;
                item.utterances_count = data.utterances_count;
                item.total_chunks = data.total_chunks;
                item.current_chunk = data.current_chunk;
                item.model = data.model;
                item.truncated = data.truncated;
                item.timestamp = new Date().toISOString();
                renderQueue();
                checkQueueScheduler();
                releaseWakeLockIfDone();
                notifyJobDone();
                loadHistoryJobs();
            } else if (data.status === 'error') {
                if (item.pollTimer) clearInterval(item.pollTimer);
                item.status = 'error';
                item.error = data.error || 'Неизвестная ошибка обработки';
                renderQueue();
                checkQueueScheduler();
                releaseWakeLockIfDone();
            } else {
                item.phase = data.phase || 'processing';
                if (data.phase_detail)        item.phase_detail = data.phase_detail;
                if (data.speakers_count)      item.speakers_count = data.speakers_count;
                if (data.utterances_count)    item.utterances_count = data.utterances_count;
                if (data.total_chunks)        item.total_chunks = data.total_chunks;
                if (data.current_chunk)       item.current_chunk = data.current_chunk;
                if (data.duration_min)        item.duration_min = data.duration_min;
                if (data.created_at)          item.created_at = data.created_at;
                if (data.aai_started_at)      item.aai_started_at = data.aai_started_at;
                if (data.drafting_started_at) item.drafting_started_at = data.drafting_started_at;
                if (data.audio_duration_sec)  item.audio_duration_sec = data.audio_duration_sec;
                renderQueue();
            }
        } catch (err) {
            failures++;
            if (failures >= MAX_FAILURES) {
                if (item.pollTimer) clearInterval(item.pollTimer);
                item.status = 'error';
                item.error = 'Связь с сервером прервана. Нажмите «Проверить статус снова», когда интернет восстановится.';
                renderQueue();
                checkQueueScheduler();
                releaseWakeLockIfDone();
            } else if (failures > 2) {
                item.reconnecting = true;
                renderQueue();
            }
        }
    };

    item.forceCheck = checkStatus;
    item.pollTimer = setInterval(checkStatus, 5000);
}

// A flat deadline cannot cover this upload: MAX_UPLOAD_BYTES is a gigabyte by
// default, which needs about half an hour on a 5 Mbit/s line, while a dead
// connection should be given up on in minutes. So watch for a *stall* instead
// of a total, and once the bytes are all sent, allow for the server writing
// them to disk before it answers.
const UPLOAD_STALL_MS = 2 * 60 * 1000;
const UPLOAD_RESPONSE_MS = 10 * 60 * 1000;

function uploadFile(item) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        const form = new FormData();
        form.append('file', item.file);
        for (const [k, v] of Object.entries(item.metadata || {})) {
            if (v) form.append(k, v);
        }

        // XHR's own timeout is a total, which is exactly what we cannot use.
        xhr.timeout = 0;
        let lastActivity = Date.now();
        let stalled = false;
        const watchdog = setInterval(() => {
            const limit = item.uploadDone ? UPLOAD_RESPONSE_MS : UPLOAD_STALL_MS;
            if (Date.now() - lastActivity > limit) {
                stalled = true;
                clearInterval(watchdog);
                xhr.abort();
            }
        }, 5000);
        const settle = (fn) => (...args) => { clearInterval(watchdog); fn(...args); };
        const done = settle(resolve);
        const fail = settle(reject);

        xhr.upload.onprogress = (e) => {
            lastActivity = Date.now();
            if (e.lengthComputable) {
                item.progress = Math.round(e.loaded / e.total * 100);
                renderQueue();
            }
        };
        xhr.upload.onloadend = () => {
            lastActivity = Date.now();
            item.progress = 100;
            item.uploadDone = true;
            renderQueue();
        };
        xhr.onload = () => {
            if (xhr.status === 401) {
                const err = new Error('Сессия истекла. Войдите снова в новой вкладке и нажмите «Попробовать снова».');
                err.code = 'AUTH';
                fail(err);
                return;
            }
            if (xhr.status === 429) {
                const err = new Error('Сервер занят другими задачами');
                err.code = 'LIMIT';
                fail(err);
                return;
            }
            if (xhr.status >= 200 && xhr.status < 300) {
                try {
                    const data = JSON.parse(xhr.responseText);
                    done(data.job_id);
                } catch (e) {
                    fail(new Error('Сервер вернул битый ответ'));
                }
            } else {
                let msg = `HTTP ${xhr.status}`;
                try {
                    const j = JSON.parse(xhr.responseText);
                    if (j.detail) msg += `: ${j.detail}`;
                } catch (_) {}
                fail(new Error(msg));
            }
        };
        xhr.onerror = () => fail(new Error('Нет соединения с сервером. Проверьте интернет.'));
        xhr.onabort = () => fail(new Error(
            stalled
                ? (item.uploadDone
                    ? 'Файл загружен, но сервер не ответил. Обновите страницу — задача могла всё же начаться.'
                    : 'Передача файла остановилась. Проверьте интернет и попробуйте снова.')
                : 'Загрузка прервана'
        ));
        xhr.open('POST', `${BACKEND}/upload`);
        xhr.send(form);
    });
}

// =========================================================================
// Queue rendering & Step-by-Step Live Log
// =========================================================================

function renderQueue() {
    const list = $('queue-list');
    if (!list) return;
    list.innerHTML = '';

    const doneCnt = queue.filter((q) => q.status === 'done').length;
    const errCnt  = queue.filter((q) => q.status === 'error').length;
    const staged = queue.filter((q) => q.status === 'staged');
    const inProgCnt = queue.length - doneCnt - errCnt - staged.length;

    if (queue.length === 0) {
        $('queue-title').textContent = 'Очередь пуста';
    } else if (staged.length > 0 && inProgCnt === 0 && doneCnt === 0 && errCnt === 0) {
        $('queue-title').textContent = staged.length > 1 ? 'Выбранные файлы' : 'Выбранный файл';
    } else if (errCnt > 0 && inProgCnt === 0 && staged.length === 0 && doneCnt === 0) {
        $('queue-title').textContent = 'Ошибка обработки';
    } else {
        const parts = [];
        if (staged.length) parts.push(`${staged.length} ожидает`);
        if (inProgCnt)     parts.push(`${inProgCnt} в работе`);
        if (doneCnt)       parts.push(`${doneCnt} готово`);
        if (errCnt)        parts.push(`${errCnt} ошибка`);
        $('queue-title').textContent = parts.length ? `Обработка — ${parts.join(', ')}` : 'Обработка';
    }

    for (const item of queue) {
        list.appendChild(renderQueueItem(item));
    }

    // Apple Style Grouped Toolbar
    const toolbar = $('queue-toolbar');
    if (toolbar) {
        toolbar.style.display = queue.length > 0 ? 'flex' : 'none';
        const startBtn = $('start-staged-btn');
        if (startBtn) {
            if (staged.length > 0) {
                startBtn.style.display = 'inline-flex';
                startBtn.textContent = staged.length > 1 ? 'Расшифровать всё' : 'Расшифровать';
                startBtn.onclick = () => {
                    staged.forEach((s) => { s.status = 'queued'; });
                    renderQueue();
                    checkQueueScheduler();
                };
            } else {
                startBtn.style.display = 'none';
            }
        }
    }
}

function getJobSteps(item) {
    const isDone = item.status === 'done';
    const isErr = item.status === 'error' || item.status === 'auth_required';
    const phase = item.phase || '';

    // Step 1: Загрузка файла
    let s1 = { title: 'Загрузка файла', state: 'pending' };
    if (item.status === 'uploading') {
        s1.state = 'active';
        s1.title = `Загрузка файла (${item.progress || 0}%)`;
    } else if (item.status === 'processing' || isDone) {
        s1.state = 'completed';
    } else if (isErr && !item.jobId) {
        s1.state = 'error';
    }

    // Step 2: Распознавание речи
    let s2 = { title: 'Распознавание речи', state: 'pending' };
    if (item.status === 'processing' && (phase === 'uploading_to_aai' || phase === 'transcribing')) {
        s2.state = 'active';
        s2.title = 'Распознавание речи...';
    } else if ((item.status === 'processing' && phase === 'drafting') || isDone) {
        s2.state = 'completed';
        const dur = item.duration_min ? `${item.duration_min} мин` : '';
        const spk = item.speakers_count ? `${item.speakers_count} спикеров` : '';
        const meta = [dur, spk].filter(Boolean).join(', ');
        s2.title = meta ? `Распознавание речи (${meta})` : 'Распознавание речи';
    } else if (isErr && (phase === 'uploading_to_aai' || phase === 'transcribing')) {
        s2.state = 'error';
    }

    // Step 3: Составление протокола
    let s3 = { title: 'Составление протокола', state: 'pending' };
    if (item.status === 'processing' && phase === 'drafting') {
        s3.state = 'active';
        if (item.total_chunks && item.total_chunks > 1) {
            s3.title = `Составление протокола (часть ${item.current_chunk || 1} из ${item.total_chunks})...`;
        } else {
            s3.title = 'Составление протокола нейросетью...';
        }
    } else if (isDone) {
        s3.state = 'completed';
        s3.title = 'Составление протокола';
    } else if (isErr && phase === 'drafting') {
        s3.state = 'error';
    }

    // Step 4: Сборка документа Word
    let s4 = { title: 'Сборка документа Word', state: 'pending' };
    if (isDone) {
        s4.state = 'completed';
    }

    return [s1, s2, s3, s4];
}

// A job the server still counts as active holds one of the account's slots.
// Dropping the card alone would leave it counted, so tell the server too.
async function cancelQueueItem(item) {
    if (!confirm(`Прервать обработку «${item.filename}»?\nЗагруженная запись будет удалена.`)) return;

    if (item.pollTimer) clearInterval(item.pollTimer);
    item.pollTimer = null;

    if (item.jobId) {
        try {
            const resp = await fetch(`${BACKEND}/jobs/${item.jobId}`, { method: 'DELETE' });
            // 404 means it is already gone, which is the outcome we wanted.
            if (!resp.ok && resp.status !== 404) {
                alert('Не удалось прервать задачу на сервере. Попробуйте ещё раз.');
                pollQueueItem(item);
                return;
            }
        } catch (err) {
            alert('Нет связи с сервером. Попробуйте ещё раз, когда интернет восстановится.');
            pollQueueItem(item);
            return;
        }
    }

    queue = queue.filter((q) => q.key !== item.key);
    if (queue.length === 0) showCard('upload-card');
    else renderQueue();
    checkQueueScheduler();
    releaseWakeLockIfDone();
    loadHistoryJobs();
}


function renderQueueItem(item) {
    const isDone = item.status === 'done';
    const isErr = item.status === 'error' || item.status === 'auth_required';
    const isWorking = item.status === 'uploading' || item.status === 'processing';
    const isStaged = item.status === 'staged';

    const wrap = document.createElement('div');
    wrap.setAttribute('data-key', item.key);
    wrap.className = 'queue-item' + (isDone ? ' done' : isErr ? ' error' : '');

    // Header line: File Name + Meta + Actions (Apple Style)
    const head = document.createElement('div');
    head.className = 'queue-item-head';

    const name = document.createElement('div');
    name.className = 'queue-name';
    name.textContent = item.filename;
    head.appendChild(name);

    // Meta (size + duration)
    const metaSpan = document.createElement('span');
    metaSpan.className = 'queue-size';
    if (isDone && item.duration_min) {
        metaSpan.textContent = `${item.sizeMB ? item.sizeMB + ' МБ · ' : ''}${item.duration_min} мин`;
    } else if (item.sizeMB && item.sizeMB !== '—') {
        metaSpan.textContent = `${item.sizeMB} МБ`;
    }
    head.appendChild(metaSpan);

    // Header Right Actions:
    if (isDone) {
        // Native Apple Style Download Pill Button in Header
        const dlBtn = document.createElement('button');
        dlBtn.className = 'queue-download-pill';
        dlBtn.title = 'Скачать протокол .docx';
        dlBtn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="7 10 12 15 17 10"></polyline><line x1="12" y1="15" x2="12" y2="3"></line></svg><span>Скачать .docx</span>';
        dlBtn.addEventListener('click', async () => {
            await downloadDocx(item.draft, makeFilename({
                metadata: item.metadata,
                timestamp: item.timestamp,
                filename: item.filename,
            }));
        });
        head.appendChild(dlBtn);
    } else if (isWorking) {
        // Live Stopwatch Badge
        if (!item.clientStartTime) {
            item.clientStartTime = item.created_at ? item.created_at * 1000 : Date.now();
        }
        const elapsed = Math.max(0, Math.floor((Date.now() - item.clientStartTime) / 1000));
        const sw = document.createElement('div');
        sw.className = 'stopwatch-badge';
        sw.textContent = formatHMS(elapsed);
        head.appendChild(sw);

        // Without this the only way out of a wedged job is to wait for the
        // server's stall timeout, with one of three slots held the whole time.
        const cancelBtn = document.createElement('button');
        cancelBtn.className = 'queue-remove-btn';
        cancelBtn.title = 'Прервать обработку';
        cancelBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
        cancelBtn.addEventListener('click', () => { cancelQueueItem(item); });
        head.appendChild(cancelBtn);
    } else if (isStaged || isErr) {
        // Remove Button
        const rmBtn = document.createElement('button');
        rmBtn.className = 'queue-remove-btn';
        rmBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
        rmBtn.title = 'Удалить';
        rmBtn.addEventListener('click', () => {
            // A failed job still has a row on the server; drop that too so the
            // card does not come back on the next reload.
            if (item.jobId) {
                fetch(`${BACKEND}/jobs/${item.jobId}`, { method: 'DELETE' }).catch(() => {});
            }
            queue = queue.filter((q) => q.key !== item.key);
            if (queue.length === 0) showCard('upload-card');
            else renderQueue();
        });
        head.appendChild(rmBtn);
    }
    wrap.appendChild(head);

    // Flat Step Tracker (For working and done files)
    if (isWorking || isDone) {
        const stepList = document.createElement('div');
        stepList.className = 'flat-step-list';

        const steps = getJobSteps(item);
        steps.forEach((step) => {
            const row = document.createElement('div');
            row.className = `flat-step-row ${step.state}`;

            const icon = document.createElement('span');
            icon.className = 'flat-step-icon';
            icon.textContent = step.state === 'completed' ? '✓' :
                               step.state === 'active' ? '●' :
                               step.state === 'error' ? '✕' : '○';
            row.appendChild(icon);

            const label = document.createElement('span');
            label.className = 'flat-step-text';
            label.textContent = step.title;
            row.appendChild(label);

            stepList.appendChild(row);
        });

        wrap.appendChild(stepList);
    }

    if (isErr && item.error) {
        const err = document.createElement('div');
        err.className = 'queue-error-msg';
        err.textContent = item.error;
        wrap.appendChild(err);
    }

    if (isDone && item.truncated) {
        // The model ran out of room. The text is real but may stop mid-hearing.
        const warn = document.createElement('div');
        warn.className = 'queue-error-msg';
        warn.textContent = 'Модель достигла предела длины ответа — проверьте, ' +
            'что протокол доведён до конца заседания.';
        wrap.appendChild(warn);
    }

    // Actions for auth expired or retry
    if (item.status === 'auth_required' || item.status === 'error') {
        const actions = document.createElement('div');
        actions.className = 'queue-actions';
        actions.style.cssText = 'margin-top: 0.75rem;';

        if (item.status === 'auth_required') {
            const loginBtn = document.createElement('button');
            loginBtn.className = 'small';
            loginBtn.textContent = 'Войти';
            loginBtn.addEventListener('click', () => {
                window.open('/login', '_blank');
            });
            actions.appendChild(loginBtn);
        }

        const retryBtn = document.createElement('button');
        retryBtn.className = 'secondary small';
        retryBtn.textContent = '↻ Попробовать снова';
        retryBtn.addEventListener('click', () => {
            item.status = 'queued';
            item.error = null;
            item.jobId = null;
            item.reconnecting = false;
            renderQueue();
            checkQueueScheduler();
        });
        actions.appendChild(retryBtn);
        wrap.appendChild(actions);
    }

    return wrap;
}

function updateProcessingItems() {
    for (const item of queue) {
        const wrap = document.querySelector(`.queue-item[data-key="${item.key}"]`);
        if (!wrap) continue;

        // Smooth continuous client-side timer
        const sw = wrap.querySelector('.stopwatch-badge');
        if (sw) {
            if (!item.clientStartTime) {
                item.clientStartTime = item.created_at ? item.created_at * 1000 : Date.now();
            }
            const elapsed = item.finalElapsed != null
                ? item.finalElapsed
                : Math.max(0, Math.floor((Date.now() - item.clientStartTime) / 1000));

            if (item.status === 'done') {
                if (item.finalElapsed == null) item.finalElapsed = elapsed;
                sw.className = 'stopwatch-badge completed';
                sw.textContent = `✓ ${formatHMS(item.finalElapsed)}`;
            } else if (item.status === 'processing' || item.status === 'uploading') {
                sw.className = 'stopwatch-badge';
                sw.textContent = formatHMS(elapsed);
            }
        }

        if (item.status !== 'processing' && item.status !== 'uploading') continue;

        // Update flat step tracker
        const stepList = wrap.querySelector('.flat-step-list');
        if (stepList) {
            const steps = getJobSteps(item);
            const rows = stepList.querySelectorAll('.flat-step-row');
            steps.forEach((step, idx) => {
                const r = rows[idx];
                if (!r) return;
                r.className = `flat-step-row ${step.state}`;
                const icon = r.querySelector('.flat-step-icon');
                const text = r.querySelector('.flat-step-text');
                if (icon) {
                    icon.textContent = step.state === 'completed' ? '✓' :
                                       step.state === 'active' ? '●' :
                                       step.state === 'error' ? '✕' : '○';
                }
                if (text) text.textContent = step.title;
            });
        }
    }
}

// Update processing timers efficiently without rebuilding DOM tree
setInterval(() => {
    if (queue.some((q) => q.status === 'processing' || q.status === 'uploading')) {
        updateProcessingItems();
    }
}, 1000);

// =========================================================================
// File picker & Drag-and-drop
// =========================================================================
const fileInput = $('file-input');
const dropZone  = $('drop-zone');

if (dropZone) {
    dropZone.addEventListener('click', () => fileInput && fileInput.click());
    dropZone.addEventListener('dragover', (e) => {
        e.preventDefault();
        dropZone.classList.add('dragover');
    });
    dropZone.addEventListener('dragleave', (e) => {
        e.preventDefault();
        dropZone.classList.remove('dragover');
    });
    dropZone.addEventListener('drop', () => {
        dropZone.classList.remove('dragover');
    });
}
const pickBtn = $('pick-btn');
if (pickBtn) pickBtn.addEventListener('click', () => fileInput && fileInput.click());
const addMoreBtn = $('add-more-btn');
if (addMoreBtn) addMoreBtn.addEventListener('click', () => fileInput && fileInput.click());

let dragCounter = 0;
const globalDropzone = $('global-dropzone');
document.addEventListener('dragenter', (e) => {
    e.preventDefault();
    dragCounter++;
    if (globalDropzone) globalDropzone.classList.add('active');
});
document.addEventListener('dragleave', (e) => {
    e.preventDefault();
    dragCounter--;
    if (dragCounter === 0 && globalDropzone) globalDropzone.classList.remove('active');
});
document.addEventListener('dragover', (e) => { e.preventDefault(); });
document.addEventListener('drop', (e) => {
    e.preventDefault();
    dragCounter = 0;
    if (globalDropzone) globalDropzone.classList.remove('active');
    if (e.dataTransfer.files.length) addFilesToQueue(e.dataTransfer.files);
});
if (fileInput) {
    fileInput.addEventListener('change', (e) => {
        if (e.target.files.length) {
            addFilesToQueue(e.target.files);
            e.target.value = '';
        }
    });
}

// Downloads & Error helper
async function fetchDraft(jobId) {
    // /jobs lists protocols without their text; pull the one being saved.
    try {
        const resp = await fetch(`${BACKEND}/status/${encodeURIComponent(jobId)}`);
        if (!resp.ok) return null;
        const data = await resp.json();
        return data.draft || null;
    } catch (e) {
        return null;
    }
}

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
        downloadTxt(text, filename);
    }
}

function downloadTxt(text, filename) {
    if (!text || !text.trim()) return;
    const txtName = filename.replace(/\.docx$/, '.txt');
    const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = txtName;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
}

// =========================================================================
// Completion Notifications (KISS Sound & Tab Title)
// =========================================================================
let originalTitle = document.title || 'Помощник секретаря';

function playCompletionChime() {
    try {
        const AudioCtx = window.AudioContext || window.webkitAudioContext;
        if (!AudioCtx) return;
        const ctx = new AudioCtx();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();

        osc.type = 'sine';
        osc.frequency.setValueAtTime(587.33, ctx.currentTime); // D5 note
        osc.frequency.exponentialRampToValueAtTime(880, ctx.currentTime + 0.15); // A5 note

        gain.gain.setValueAtTime(0.12, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.45);

        osc.connect(gain);
        gain.connect(ctx.destination);

        osc.start();
        osc.stop(ctx.currentTime + 0.45);
    } catch (e) {
        // Ignore autoplay policy restrictions
    }
}

function notifyJobDone() {
    playCompletionChime();
    const doneCnt = queue.filter((q) => q.status === 'done').length;
    if (document.hidden && doneCnt > 0) {
        document.title = `(${doneCnt}) Готово — ${originalTitle}`;
    }
}

// =========================================================================
// History & Auto-Resume Persistence
// =========================================================================
let historyJobs = [];

async function loadHistoryJobs() {
    try {
        const resp = await fetch(`${BACKEND}/jobs`);
        if (!resp.ok) return;
        const data = await resp.json();
        const allJobs = data.jobs || [];
        
        historyJobs = allJobs.filter((j) => j.status === 'done');
        renderHistory();

        // Auto-resume active/processing jobs upon page load or reload
        const activeRemote = allJobs.filter((j) => j.status === 'processing');
        let queueChanged = false;
        for (const remote of activeRemote) {
            const exists = queue.some((q) => q.jobId === remote.id || q.key === remote.id);
            if (!exists) {
                const item = {
                    key: remote.id,
                    jobId: remote.id,
                    filename: remote.filename || 'Аудиозапись',
                    sizeMB: '—',
                    metadata: remote.metadata || {},
                    status: 'processing',
                    phase: remote.phase || 'processing',
                    phase_detail: remote.phase_detail,
                    speakers_count: remote.speakers_count,
                    utterances_count: remote.utterances_count,
                    total_chunks: remote.total_chunks,
                    current_chunk: remote.current_chunk,
                    duration_min: remote.duration_min,
                    created_at: remote.created_at,
                    pollStart: Date.now()
                };
                queue.push(item);
                pollQueueItem(item);
                queueChanged = true;
            }
        }
        if (queueChanged) {
            showCard('queue-card');
            renderQueue();
            acquireWakeLock();
        }
    } catch (e) {
        console.warn('Failed to load history jobs:', e);
    }
}

function renderHistory() {
    const container = $('history-list');
    if (!container) return;
    container.innerHTML = '';
    
    if (!historyJobs || historyJobs.length === 0) {
        container.innerHTML = '<div class="history-empty">Пока нет готовых протоколов</div>';
        return;
    }

    historyJobs.forEach((job) => {
        const itemEl = document.createElement('div');
        itemEl.className = 'history-item';
        
        const contentEl = document.createElement('div');
        contentEl.className = 'history-item-content';

        const titleEl = document.createElement('div');
        titleEl.className = 'history-item-title';
        const nameText = makeFilename(job);
        titleEl.textContent = nameText;

        const metaEl = document.createElement('div');
        metaEl.className = 'history-item-meta';
        const dateStr = job.updated_at
            ? new Date(job.updated_at * 1000).toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' })
            : '';
        const durStr = job.duration_min ? `${job.duration_min} мин` : '';
        const warnStr = job.truncated ? 'возможно неполный' : '';
        metaEl.textContent = [durStr, dateStr, warnStr].filter(Boolean).join(' · ');

        contentEl.appendChild(titleEl);
        contentEl.appendChild(metaEl);

        const menuWrapper = document.createElement('div');
        menuWrapper.className = 'item-dropdown-wrapper';

        const dotsBtn = document.createElement('button');
        dotsBtn.className = 'history-dots-btn';
        dotsBtn.title = 'Опции';
        dotsBtn.innerHTML = `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <circle cx="12" cy="12" r="1.5"></circle>
            <circle cx="12" cy="5" r="1.5"></circle>
            <circle cx="12" cy="19" r="1.5"></circle>
        </svg>`;

        const dropdownMenu = document.createElement('div');
        dropdownMenu.className = 'item-dropdown';
        dropdownMenu.hidden = true;

        const dlItemBtn = document.createElement('button');
        dlItemBtn.className = 'item-dropdown-btn';
        dlItemBtn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
            <polyline points="7 10 12 15 17 10"></polyline>
            <line x1="12" y1="15" x2="12" y2="3"></line>
        </svg><span>Скачать</span>`;
        
        dlItemBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            dropdownMenu.hidden = true;
            const draft = job.draft || await fetchDraft(job.id);
            if (!draft) {
                alert('Не удалось загрузить текст протокола. Проверьте соединение.');
                return;
            }
            job.draft = draft;
            await downloadDocx(draft, nameText);
        });

        const delItemBtn = document.createElement('button');
        delItemBtn.className = 'item-dropdown-btn danger';
        delItemBtn.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="3 6 5 6 21 6"></polyline>
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
        </svg><span>Удалить</span>`;
        
        delItemBtn.addEventListener('click', async (e) => {
            e.stopPropagation();
            dropdownMenu.hidden = true;
            if (!confirm(`Удалить протокол «${nameText}»?\nВосстановить его будет невозможно.`)) return;
            try {
                const resp = await fetch(`${BACKEND}/jobs/${job.id}`, { method: 'DELETE' });
                if (resp.ok) {
                    historyJobs = historyJobs.filter(j => j.id !== job.id);
                    renderHistory();
                    if (typeof fetchUserProfile === 'function') fetchUserProfile();
                } else {
                    alert('Ошибка при удалении');
                }
            } catch (err) {
                alert('Ошибка соединения');
            }
        });

        dotsBtn.addEventListener('click', (e) => {
            e.stopPropagation();
            const isHidden = dropdownMenu.parentElement !== document.body;
            
            // Clean up any previously opened dropdowns
            document.querySelectorAll('.item-dropdown').forEach(d => {
                d.hidden = true;
                if (d.parentElement === document.body) document.body.removeChild(d);
            });

            if (isHidden) {
                dropdownMenu.hidden = false;
                dropdownMenu.style.left = '-9999px';
                dropdownMenu.style.top = '-9999px';
                document.body.appendChild(dropdownMenu);
                
                const rect = dotsBtn.getBoundingClientRect();
                const dpWidth = dropdownMenu.offsetWidth || 140;
                
                dropdownMenu.style.top = `${rect.bottom + window.scrollY + 6}px`;
                dropdownMenu.style.left = `${rect.right + window.scrollX - dpWidth}px`;
            }
        });

        // We append the menu items to the dropdown, but do NOT append the dropdown to menuWrapper yet.
        dropdownMenu.appendChild(dlItemBtn);
        dropdownMenu.appendChild(delItemBtn);
        menuWrapper.appendChild(dotsBtn);

        itemEl.appendChild(contentEl);
        itemEl.appendChild(menuWrapper);
        container.appendChild(itemEl);
    });
}

// =========================================================================
// Profile & Dropdown Management
// =========================================================================
async function fetchUserProfile() {
    try {
        const resp = await fetch(`${BACKEND}/api/me`);
        if (!resp.ok) return;
        const user = await resp.json();

        const name = user.display_name || user.username || 'Пользователь';
        const initial = name.charAt(0).toUpperCase();

        const btnEl = $('avatar-btn');
        const nameEl = $('dropdown-user-name');
        const planEl = $('dropdown-user-plan');
        const statProto = $('dropdown-stat-protocols');
        const statSavedShort = $('dropdown-stat-saved-short');
        const bannerSaved = $('dropdown-saved-banner');

        if (btnEl) btnEl.textContent = initial;
        if (nameEl) nameEl.textContent = name;
        if (planEl) planEl.textContent = `${user.plan || 'Персональный'} доступ`;
        if (statProto) statProto.textContent = user.total_protocols || 0;

        if (Number.isInteger(user.max_active_jobs) && user.max_active_jobs > 0) {
            maxActiveJobs = user.max_active_jobs;
            checkQueueScheduler();
        }

        const totalMin = user.total_duration_min || 0;
        const savedMinTotal = Math.round(totalMin * 3.5);

        let shortStr = '0 мин';
        let bannerStr = '⚡ Готов экономить ваше время';

        if (savedMinTotal > 0) {
            if (savedMinTotal < 60) {
                shortStr = `${savedMinTotal} мин`;
                bannerStr = `⚡ Сберегли ${savedMinTotal} минут вашей работы`;
            } else {
                const h = Math.floor(savedMinTotal / 60);
                const m = savedMinTotal % 60;
                const decimalH = (savedMinTotal / 60).toFixed(1);
                shortStr = `~${decimalH} ч`;
                if (m > 0) {
                    bannerStr = `⚡ Сберегли ${h} ч ${m} мин вашей работы`;
                } else {
                    bannerStr = `⚡ Сберегли ${h} ч вашей работы`;
                }
            }
        }

        if (statSavedShort) statSavedShort.textContent = shortStr;
        if (bannerSaved) bannerSaved.textContent = bannerStr;
    } catch (e) {
        console.warn('Failed to fetch user profile:', e);
    }
}

function initProfileDropdown() {
    const btn = $('avatar-btn');
    const dropdown = $('profile-dropdown');

    if (btn && dropdown) {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            const isHidden = dropdown.hasAttribute('hidden');
            if (isHidden) {
                dropdown.removeAttribute('hidden');
                fetchUserProfile();
            } else {
                dropdown.setAttribute('hidden', '');
            }
        });

        document.addEventListener('click', (e) => {
            if (!dropdown.contains(e.target) && e.target !== btn) {
                dropdown.setAttribute('hidden', '');
            }
        });
    }
}

// =========================================================================
// Dictaphone & IndexedDB Autosave
// =========================================================================
let mediaRecorder = null;
let audioChunks = [];
let recordingInterval = null;
let recordingStartTime = 0;
let db = null;
let isRecording = false;

// Initialize IndexedDB
const requestDB = indexedDB.open('DictaphoneDB', 1);
requestDB.onupgradeneeded = (e) => {
    db = e.target.result;
    if (!db.objectStoreNames.contains('chunks')) {
        db.createObjectStore('chunks', { autoIncrement: true });
    }
};
requestDB.onsuccess = (e) => {
    db = e.target.result;
    checkOrphanedRecording();
};
requestDB.onerror = (e) => console.warn('IndexedDB error:', e);

function clearChunksDB() {
    if (!db) return;
    const tx = db.transaction('chunks', 'readwrite');
    tx.objectStore('chunks').clear();
}

function saveChunkToDB(blob) {
    if (!db) return;
    const tx = db.transaction('chunks', 'readwrite');
    tx.objectStore('chunks').add(blob);
}

function checkOrphanedRecording() {
    if (!db) return;
    const tx = db.transaction('chunks', 'readonly');
    const store = tx.objectStore('chunks');
    const getReq = store.getAll();
    getReq.onsuccess = () => {
        if (getReq.result && getReq.result.length > 0) {
            console.log('Found orphaned recording chunks, recovering...');
            const firstType = getReq.result[0].type || 'audio/webm';
            const ext = firstType.includes('mp4') ? 'mp4' : 'webm';
            const recoveredBlob = new Blob(getReq.result, { type: firstType });
            const recoveredFile = new File([recoveredBlob], `Восстановленная_запись_${new Date().toISOString().slice(0,10)}.${ext}`, { type: firstType });
            addFilesToQueue([recoveredFile]);
            setTimeout(clearChunksDB, 100);
        }
    };
}

function updateRecordingTimer() {
    const elapsed = Math.floor((Date.now() - recordingStartTime) / 1000);
    const h = Math.floor(elapsed / 3600);
    const m = Math.floor((elapsed % 3600) / 60);
    const s = elapsed % 60;
    const pad = (n) => String(n).padStart(2, '0');
    const timerEl = $('recording-timer');
    if (timerEl) timerEl.textContent = h > 0 ? `${pad(h)}:${pad(m)}:${pad(s)}` : `00:${pad(m)}:${pad(s)}`;
}

async function startRecording() {
    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        mediaRecorder = new MediaRecorder(stream);
        audioChunks = [];
        clearChunksDB();

        mediaRecorder.ondataavailable = (e) => {
            if (e.data.size > 0) {
                audioChunks.push(e.data);
                saveChunkToDB(e.data);
            }
        };

        mediaRecorder.onstop = () => {
            clearInterval(recordingInterval);
            isRecording = false;
            releaseWakeLock();
            stream.getTracks().forEach(t => t.stop());
            
            const defActions = $('default-actions');
            const recUI = $('recording-ui');
            if (defActions) defActions.hidden = false;
            if (recUI) recUI.hidden = true;
            
            const timerEl = $('recording-timer');
            if (timerEl) timerEl.textContent = '00:00:00';

            const mime = mediaRecorder.mimeType || 'audio/webm';
            const ext = mime.includes('mp4') ? 'mp4' : 'webm';
            const audioBlob = new Blob(audioChunks, { type: mime });
            const dateStr = new Date().toISOString().slice(0, 16).replace('T', '_').replace(':', '-');
            const file = new File([audioBlob], `Запись_${dateStr}.${ext}`, { type: mime });
            addFilesToQueue([file]);
            setTimeout(clearChunksDB, 100);
        };

        mediaRecorder.start(1000); // chunk every 1 second
        isRecording = true;
        recordingStartTime = Date.now();
        updateRecordingTimer();
        recordingInterval = setInterval(updateRecordingTimer, 1000);
        
        acquireWakeLock();
        
        const defActions = $('default-actions');
        const recUI = $('recording-ui');
        if (defActions) defActions.hidden = true;
        if (recUI) recUI.hidden = false;
        
    } catch (err) {
        alert('Не удалось получить доступ к микрофону. Разрешите доступ в настройках браузера.');
        console.error('Microphone error:', err);
    }
}

function stopRecording() {
    if (mediaRecorder && mediaRecorder.state !== 'inactive') {
        mediaRecorder.stop();
    }
}

const recordBtn = $('record-btn');
const stopRecordBtn = $('stop-record-btn');
if (recordBtn) recordBtn.addEventListener('click', startRecording);
if (stopRecordBtn) stopRecordBtn.addEventListener('click', stopRecording);

window.addEventListener('beforeunload', (e) => {
    if (isRecording) {
        e.preventDefault();
        e.returnValue = 'Запись прервется. Вы уверены?';
    }
});

document.addEventListener('visibilitychange', () => {
    if (document.hidden && isRecording) {
        if (mediaRecorder && mediaRecorder.state === 'recording') {
            mediaRecorder.requestData();
        }
    }
});

// Initial load on page startup
loadHistoryJobs();
fetchUserProfile();
initProfileDropdown();

document.addEventListener('click', () => {
    document.querySelectorAll('.item-dropdown').forEach(d => {
        d.hidden = true;
        if (d.parentElement === document.body) document.body.removeChild(d);
    });
});

window.addEventListener('scroll', () => {
    document.querySelectorAll('.item-dropdown').forEach(d => {
        d.hidden = true;
        if (d.parentElement === document.body) document.body.removeChild(d);
    });
}, { capture: true });

// Re-check jobs immediately on tab activation / unlock / online
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
        document.title = originalTitle;
        acquireWakeLock();
        loadHistoryJobs();
        fetchUserProfile();
        queue.forEach((item) => {
            if (item.status === 'processing' && typeof item.forceCheck === 'function') {
                item.forceCheck();
            }
        });
    }
});

window.addEventListener('online', () => {
    loadHistoryJobs();
    fetchUserProfile();
    queue.forEach((item) => {
        if (item.status === 'processing' && typeof item.forceCheck === 'function') {
            item.forceCheck();
        }
    });
});
