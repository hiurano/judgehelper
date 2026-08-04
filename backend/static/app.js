'use strict';

const BACKEND = window.location.origin;

// 401 from a protected endpoint = session expired.
let sessionExpired = false;
const _origFetch = window.fetch.bind(window);
window.fetch = async function (...args) {
    const resp = await _origFetch(...args);
    if (resp.status === 401) {
        sessionExpired = true;
    }
    return resp;
};

const $ = (id) => document.getElementById(id);

// =========================================================================
// Card switcher
// =========================================================================
const ALL_CARDS = ['upload-card', 'queue-card', 'error-card'];
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
            status: 'queued',
            progress: 0,
        };
        queue.push(item);
    }
    showCard('queue-card');
    renderQueue();
    checkQueueScheduler();
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
        if (item.status === 'done' || item.status === 'error' || item.status === 'transcribed' || item.status === 'auth_required') {
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
                item.draft = data.draft;
                item.transcript = data.transcript;
                item.duration_min = data.duration_min;
                item.model = data.model;
                item.timestamp = new Date().toISOString();
                renderQueue();
                checkQueueScheduler();
                releaseWakeLockIfDone();
            } else if (data.status === 'transcribed') {
                if (item.pollTimer) clearInterval(item.pollTimer);
                item.status = 'transcribed';
                item.transcript = data.transcript;
                item.duration_min = data.duration_min;
                renderQueue();
                checkQueueScheduler();
                releaseWakeLockIfDone();
            } else if (data.status === 'error') {
                if (item.pollTimer) clearInterval(item.pollTimer);
                item.status = 'error';
                item.error = data.error || 'Неизвестная ошибка обработки';
                renderQueue();
                checkQueueScheduler();
                releaseWakeLockIfDone();
            } else {
                item.phase = data.phase || 'processing';
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
// Queue rendering & ETA calculations
// =========================================================================
function phaseLabel(phase) {
    return {
        'uploading_to_aai': 'Передача файла на сервер…',
        'transcribing':     'Расшифровка аудио…',
        'drafting':         'Составление протокола нейросетью…',
    }[phase] || 'Обработка…';
}

function estimateTranscribing(item) {
    const nowSec = Date.now() / 1000;
    const startSec = item.aai_started_at || item.created_at || (item.pollStart ? item.pollStart / 1000 : nowSec);
    const elapsedSec = Math.max(0, Math.round(nowSec - startSec));

    // Determine audio length (from AAI or approximate ~2 min per MB for m4a/mp3)
    let audioSec = item.audio_duration_sec;
    if (!audioSec && item.sizeMB && item.sizeMB !== '—') {
        const mb = parseFloat(item.sizeMB);
        if (!isNaN(mb) && mb > 0) {
            audioSec = mb * 120;
        }
    }

    // AssemblyAI Universal-2 takes ~20-25% of audio duration (no artificial 600s ceiling)
    const estimatedSec = audioSec ? Math.max(30, Math.round(audioSec * 0.25)) : 180;

    let pct = 0;
    if (elapsedSec <= estimatedSec) {
        pct = Math.max(5, Math.round((elapsedSec / estimatedSec) * 90));
    } else {
        // Smooth asymptotic progress above 90% towards 99%
        const overtime = elapsedSec - estimatedSec;
        const extra = 9 * (1 - Math.exp(-overtime / (estimatedSec * 0.5 || 60)));
        pct = Math.min(99, Math.round(90 + extra));
    }
    return { elapsedSec, estimatedSec, pct, isOvertime: elapsedSec > estimatedSec };
}

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
    if (!list) return;
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

function renderStagedBar() {
    let bar = $('staged-bar');
    if (!bar) {
        bar = document.createElement('div');
        bar.id = 'staged-bar';
        bar.style.cssText = 'padding:1.5rem 0 0; margin-top:1.5rem; border-top:1px solid var(--border); display:flex; align-items:center; gap:1rem; flex-wrap:wrap';
        const list = $('queue-list');
        if (list && list.parentNode) list.parentNode.insertBefore(bar, list.nextSibling);
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
        staged.forEach((s) => { s.status = 'queued'; });
        renderQueue();
        checkQueueScheduler();
    });
    bar.appendChild(goBtn);
}

function renderMergeBar() {
    let bar = $('merge-bar');
    if (!bar) {
        bar = document.createElement('div');
        bar.id = 'merge-bar';
        bar.style.cssText = 'padding:1.5rem 0 0; margin-top:1.5rem; border-top:1px solid var(--border); display:flex; align-items:center; gap:1rem; flex-wrap:wrap';
        const list = $('queue-list');
        if (list && list.parentNode) list.parentNode.insertBefore(bar, list.nextSibling);
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
    queue.forEach((q) => q.selected = false);

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
            queue = queue.filter((q) => q.key !== item.key);
            if (queue.length === 0) showCard('upload-card');
            else renderQueue();
        });
        head.appendChild(rmBtn);
    }
    
    if (item.status === 'done') {
        const btns = document.createElement('div');
        btns.style.cssText = 'margin-left: auto; display: flex; gap: 0.5rem;';
        
        const txtBtn = document.createElement('button');
        txtBtn.className = 'small secondary';
        txtBtn.textContent = 'Сырой текст (.txt)';
        txtBtn.addEventListener('click', () => {
            downloadTxt(item.transcript, makeFilename({
                metadata: item.metadata,
                timestamp: item.timestamp,
                filename: item.filename,
            }));
        });
        btns.appendChild(txtBtn);

        const dlBtn = document.createElement('button');
        dlBtn.className = 'small';
        dlBtn.textContent = 'Готовый .docx';
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
        btns.appendChild(dlBtn);
        
        head.appendChild(btns);
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
        
        if (item.reconnecting) {
            t.style.color = '#f59e0b';
            t.textContent = `Связь с сервером восстанавливается… (${formatHMS(elapsed)})`;
        } else if (item.phase === 'uploading_to_aai') {
            t.textContent = `Передача на сервер расшифровки… (${formatHMS(elapsed)})`;
        } else if (item.phase === 'transcribing') {
            const est = estimateTranscribing(item);
            if (est) {
                if (est.isOvertime) {
                    t.textContent = `Расшифровка аудио: ${est.pct}% · ${formatHMS(est.elapsedSec)} (завершение…)`;
                } else {
                    t.textContent = `Расшифровка аудио: ${est.pct}% · ${formatHMS(est.elapsedSec)} из ≈${formatHMS(est.estimatedSec)}`;
                }
            } else {
                t.textContent = `Расшифровка аудио… (${formatHMS(elapsed)})`;
            }
        } else if (item.phase === 'drafting') {
            t.textContent = `Составление протокола нейросетью… (${formatHMS(elapsed)})`;
        } else {
            t.textContent = phaseLabel(item.phase) + (elapsed ? ` (${formatHMS(elapsed)})` : '');
        }
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

    // Progress bar
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
        if (item.phase === 'transcribing') {
            const est = estimateTranscribing(item);
            if (est) {
                prBar.style.width = est.pct + '%';
            } else {
                prBar.classList.add('indeterminate');
            }
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

    // Actions for auth expired or retry
    const actions = document.createElement('div');
    actions.className = 'queue-actions';

    if (item.status === 'auth_required') {
        const loginBtn = document.createElement('button');
        loginBtn.className = 'small';
        loginBtn.textContent = 'Войти';
        loginBtn.addEventListener('click', () => {
            window.open('/login', '_blank');
        });
        actions.appendChild(loginBtn);

        const retryBtn = document.createElement('button');
        retryBtn.className = 'secondary small';
        retryBtn.textContent = '↻ Попробовать снова';
        retryBtn.addEventListener('click', () => {
            sessionExpired = false;
            item.status = 'queued';
            renderQueue();
            checkQueueScheduler();
        });
        actions.appendChild(retryBtn);
    } else if (item.status === 'error' && item.jobId) {
        const checkBtn = document.createElement('button');
        checkBtn.className = 'secondary small';
        checkBtn.textContent = '↻ Проверить статус снова';
        checkBtn.addEventListener('click', () => {
            item.status = 'processing';
            item.error = null;
            item.reconnecting = false;
            renderQueue();
            pollQueueItem(item);
            if (typeof item.forceCheck === 'function') {
                item.forceCheck();
            }
        });
        actions.appendChild(checkBtn);
    }

    if (actions.children.length) wrap.appendChild(actions);

    // Expanded view: editable draft
    if (item.expanded && item.status === 'done') {
        const warn = document.createElement('div');
        warn.className = 'warning';
        warn.innerHTML = '<strong>Это черновик.</strong> Правки сохраняются — скачайте .docx чтобы они попали в файл.';
        wrap.appendChild(warn);

        const ta = document.createElement('textarea');
        ta.id = 'draft-' + item.key;
        ta.spellcheck = false;
        ta.value = item.draft;
        ta.addEventListener('input', () => {
            item.draft = ta.value;
        });
        wrap.appendChild(ta);
    }

    return wrap;
}

// Update processing timers
setInterval(() => {
    const editing = document.activeElement && (
        document.activeElement.tagName === 'TEXTAREA' ||
        document.activeElement.tagName === 'INPUT'
    );
    if (editing) return;
    if (queue.some((q) => q.status === 'processing')) renderQueue();
}, 1000);

// =========================================================================
// File picker & Drag-and-drop
// =========================================================================
const fileInput = $('file-input');
const dropZone  = $('drop-zone');

if (dropZone) dropZone.addEventListener('click', () => fileInput && fileInput.click());
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

// =========================================================================
// Queue controls & Downloads
// =========================================================================
const clearBtn = $('clear-queue-btn');
if (clearBtn) {
    clearBtn.addEventListener('click', () => {
        queue.forEach((q) => { if (q.pollTimer) clearInterval(q.pollTimer); });
        queue = [];
        showCard('upload-card');
    });
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

function showError(msg) {
    releaseWakeLock();
    showCard('error-card');
    const el = $('error-text');
    if (el) el.textContent = msg;
}

// ServiceWorker cleanup
if ('serviceWorker' in navigator) {
    navigator.serviceWorker.getRegistrations().then((registrations) => {
        for (const r of registrations) {
            r.unregister();
        }
    });
}

// Re-check jobs immediately on tab activation / unlock / online
document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
        acquireWakeLock();
        queue.forEach((item) => {
            if (item.status === 'processing' && typeof item.forceCheck === 'function') {
                item.forceCheck();
            }
        });
    }
});

window.addEventListener('online', () => {
    queue.forEach((item) => {
        if (item.status === 'processing' && typeof item.forceCheck === 'function') {
            item.forceCheck();
        }
    });
});