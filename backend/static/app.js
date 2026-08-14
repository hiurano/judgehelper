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

function checkQueueScheduler() {
    const uploadingCount = queue.filter((q) => q.status === 'uploading').length;
    if (uploadingCount >= 2) return;
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
        checkQueueScheduler();
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
                item.draft = data.draft;
                item.transcript = data.transcript;
                item.duration_min = data.duration_min;
                item.model = data.model;
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
function estimateTranscribing(item) {
    const nowSec = Date.now() / 1000;
    const startSec = item.aai_started_at || item.created_at || (item.pollStart ? item.pollStart / 1000 : nowSec);
    const elapsedSec = Math.max(0, Math.round(nowSec - startSec));

    let audioSec = item.audio_duration_sec;
    if (!audioSec && item.sizeMB && item.sizeMB !== '—') {
        const mb = parseFloat(item.sizeMB);
        if (!isNaN(mb) && mb > 0) {
            audioSec = mb * 120;
        }
    }

    const estimatedSec = audioSec ? Math.max(30, Math.round(audioSec * 0.25)) : 180;

    let pct = 0;
    if (elapsedSec <= estimatedSec) {
        pct = Math.max(5, Math.round((elapsedSec / estimatedSec) * 90));
    } else {
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

        if (stagedCnt > 0 && inProgCnt === 0 && doneCnt === 0 && errCnt === 0) {
            $('queue-title').textContent = stagedCnt > 1 ? 'Выбранные файлы' : 'Выбранный файл';
        } else if (errCnt > 0 && inProgCnt === 0 && stagedCnt === 0 && doneCnt === 0) {
            $('queue-title').textContent = 'Ошибка обработки';
        } else {
            const parts = [];
            if (stagedCnt) parts.push(`${stagedCnt} ожидает`);
            if (inProgCnt) parts.push(`${inProgCnt} в работе`);
            if (doneCnt)   parts.push(`${doneCnt} готово`);
            if (errCnt)    parts.push(`${errCnt} ошибка`);
            $('queue-title').textContent = parts.length ? `Обработка — ${parts.join(', ')}` : 'Обработка';
        }
    }
    for (const item of queue) {
        list.appendChild(renderQueueItem(item));
    }
    renderStagedBar();
}

function renderStagedBar() {
    let bar = $('staged-bar');
    if (!bar) {
        bar = document.createElement('div');
        bar.id = 'staged-bar';
        bar.style.cssText = 'display:flex; align-items:center; gap:1rem; flex-wrap:wrap';
        const container = $('staged-bar-container') || $('queue-card');
        if (container) container.appendChild(bar);
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

function getOverallProgressInfo(item) {
    let audioSec = item.audio_duration_sec;
    if (!audioSec && item.sizeMB && item.sizeMB !== '—') {
        const mb = parseFloat(item.sizeMB);
        if (!isNaN(mb) && mb > 0) audioSec = mb * 120;
    }
    const totalEstSec = audioSec ? Math.max(45, Math.round(audioSec * 0.25 + 35)) : 180;
    const totalElapsed = item.created_at ? Math.max(0, Math.round(Date.now() / 1000 - item.created_at)) : phaseElapsedSec(item);
    const remainingSec = Math.max(0, totalEstSec - totalElapsed);
    const timeStr = (totalElapsed < totalEstSec && remainingSec > 0)
        ? `осталось ≈ ${formatHMS(remainingSec)}`
        : `завершение… (${formatHMS(totalElapsed)})`;

    if (item.status === 'uploading') {
        const uploadPct = Math.min(99, item.progress || 0);
        const overallPct = Math.max(3, Math.round(uploadPct * 0.15));
        return {
            stageText: 'Загрузка файла на сервер…',
            timeText: timeStr,
            pct: overallPct
        };
    }

    if (item.status === 'processing') {
        if (item.reconnecting) {
            return {
                stageText: 'Восстановление связи…',
                timeText: timeStr,
                pct: 50
            };
        }

        if (item.phase === 'uploading_to_aai') {
            return {
                stageText: 'Передача в нейросеть…',
                timeText: timeStr,
                pct: 20
            };
        }

        if (item.phase === 'transcribing') {
            const est = estimateTranscribing(item);
            let transPct = 40;
            if (est) {
                transPct = Math.round(25 + (est.pct * 0.55));
            }
            return {
                stageText: 'Расшифровка аудио…',
                timeText: timeStr,
                pct: transPct
            };
        }

        if (item.phase === 'drafting') {
            const draftElapsed = item.drafting_started_at
                ? Math.round(Date.now() / 1000 - item.drafting_started_at)
                : phaseElapsedSec(item);
            const draftPct = Math.min(96, Math.round(80 + (draftElapsed / 40) * 16));
            return {
                stageText: 'Составление протокола нейросетью…',
                timeText: timeStr,
                pct: draftPct
            };
        }

        return {
            stageText: 'Обработка…',
            timeText: timeStr,
            pct: 50
        };
    }

    if (item.status === 'done') {
        return { stageText: `Готово · ${item.duration_min ?? '—'} мин`, timeText: '', pct: 100 };
    }
    if (item.status === 'staged') {
        return { stageText: 'Готов к обработке', timeText: '', pct: 0 };
    }
    if (item.status === 'error') {
        return { stageText: 'Ошибка обработки', timeText: '', pct: 0 };
    }
    return { stageText: 'В очереди…', timeText: '', pct: 0 };
}

function renderQueueItem(item) {
    const wrap = document.createElement('div');
    wrap.setAttribute('data-key', item.key);
    wrap.className = 'queue-item ' + (
        item.status === 'done' ? 'done' :
        (item.status === 'error' || item.status === 'auth_required') ? 'error' : ''
    );

    const head = document.createElement('div');
    head.className = 'queue-item-head';

    const name = document.createElement('div');
    name.className = 'queue-name';
    name.textContent = item.filename;
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
        rmBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line></svg>';
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

        const dlBtn = document.createElement('button');
        dlBtn.className = 'small';
        dlBtn.textContent = 'Скачать .docx';
        dlBtn.addEventListener('click', async () => {
            await downloadDocx(item.draft, makeFilename({
                metadata: item.metadata,
                timestamp: item.timestamp,
                filename: item.filename,
            }));
        });
        btns.appendChild(dlBtn);
        head.appendChild(btns);
    }
    
    wrap.appendChild(head);

    // Status row (Left: text, Right: remaining time)
    const status = document.createElement('div');
    status.className = 'queue-status';
    status.style.cssText = 'display: flex; justify-content: space-between; align-items: center; gap: 0.5rem; margin-top: 0.4rem; font-size: 0.875rem;';

    const info = getOverallProgressInfo(item);
    const stageSpan = document.createElement('span');
    stageSpan.className = 'queue-stage-text';
    const timeSpan = document.createElement('span');
    timeSpan.className = 'queue-time-text';
    timeSpan.style.cssText = 'color: var(--text-muted); font-size: 0.825rem; white-space: nowrap;';

    if (item.status === 'done') {
        stageSpan.className = 'badge-done queue-stage-text';
        stageSpan.innerHTML = `Готово · ${item.duration_min ?? '—'} мин · ${(item.draft?.length || 0).toLocaleString('ru')} символов`;
    } else if (item.status === 'error') {
        stageSpan.className = 'badge-error queue-stage-text';
        stageSpan.textContent = 'Ошибка';
    } else if (item.status === 'staged') {
        stageSpan.style.color = 'var(--text-muted)';
        stageSpan.textContent = 'Готов к обработке';
    } else {
        stageSpan.textContent = info.stageText;
        timeSpan.textContent = info.timeText;
    }

    status.appendChild(stageSpan);
    if (info.timeText && item.status !== 'done' && item.status !== 'error') {
        status.appendChild(timeSpan);
    }
    wrap.appendChild(status);

    // Continuous progress bar
    if (item.status === 'uploading' || item.status === 'processing') {
        const prWrap = document.createElement('div');
        prWrap.className = 'custom-progress';
        prWrap.style.cssText = 'margin-top: 0.5rem; background: rgba(255,255,255,0.08); height: 6px; border-radius: 3px; overflow: hidden;';
        
        const prBar = document.createElement('div');
        prBar.className = 'custom-progress-bar';
        prBar.style.cssText = `width: ${info.pct}%; height: 100%; background: var(--accent-primary); border-radius: 3px; transition: width 0.4s ease;`;
        
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
            item.status = 'queued';
            renderQueue();
            checkQueueScheduler();
        });
        actions.appendChild(retryBtn);
    } else if (item.status === 'error') {
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
    }

    if (actions.children.length) wrap.appendChild(actions);
    return wrap;
}

function updateProcessingItems() {
    for (const item of queue) {
        if (item.status !== 'processing' && item.status !== 'uploading') continue;
        const wrap = document.querySelector(`.queue-item[data-key="${item.key}"]`);
        if (!wrap) continue;
        const info = getOverallProgressInfo(item);
        const stageSpan = wrap.querySelector('.queue-stage-text');
        const timeSpan = wrap.querySelector('.queue-time-text');
        const prBar = wrap.querySelector('.custom-progress-bar');

        if (stageSpan) stageSpan.textContent = info.stageText;
        if (timeSpan) timeSpan.textContent = info.timeText;
        if (prBar) prBar.style.width = info.pct + '%';
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
        document.title = `🔔 (${doneCnt}) Готово! — ${originalTitle}`;
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
                    created_at: remote.created_at,
                    progress: 50,
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
        metaEl.textContent = [durStr, dateStr].filter(Boolean).join(' · ');

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
            await downloadDocx(job.draft, nameText);
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