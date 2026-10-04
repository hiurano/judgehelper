/* Source and proposed changes are always rendered as text, never as HTML. */
(() => {
    const dialog = document.getElementById('protocol-review');
    const content = document.getElementById('review-content');
    const status = document.getElementById('review-status');
    let jobId, data, edits, dirty = false, page = 0, loading = 0, busy = false;
    const audio = document.getElementById('review-audio');
    let audioUrl = null;
    const pageSize = 30;

    function setAudio(file) {
        audio.pause();
        audio.removeAttribute('src');
        if (audioUrl) URL.revokeObjectURL(audioUrl);
        audioUrl = file ? URL.createObjectURL(file) : null;
        if (audioUrl) audio.src = audioUrl;
        document.getElementById('review-audio-name').textContent = file?.name || '';
        audio.load();
    }
    document.getElementById('review-audio-file').onchange = event => setAudio(event.target.files[0]);

    function node(tag, text, className) {
        const el = document.createElement(tag);
        if (text !== undefined) el.textContent = text;
        if (className) el.className = className;
        return el;
    }
    function button(text, handler) {
        const el = node('button', text, 'secondary');
        el.type = 'button';
        el.onclick = handler;
        return el;
    }
    function changed() {
        dirty = true;
        edits.reviewed = false;
        status.textContent = 'Есть несохранённые изменения';
    }
    async function api(path = '', options = {}) {
        const response = await fetch(`/jobs/${encodeURIComponent(jobId)}/review${path}`, options);
        if (!response.ok) {
            let message = 'Не удалось выполнить запрос';
            try {
                const body = await response.json();
                if (typeof body.detail === 'string') message = body.detail;
            } catch (_) { /* Keep the readable default. */ }
            throw new Error(message);
        }
        return response;
    }
    function setData(value) {
        data = value;
        edits = structuredClone(Object.fromEntries([
            'revision', 'decisions', 'speakers', 'utterances', 'fields', 'manual_text',
            'resolved_concerns', 'reviewed',
        ].map(key => [key, data[key]])));
        dirty = false;
        render();
    }
    async function reload() {
        if (dirty && !confirm('Отменить несохранённые изменения и загрузить документ заново?')) return;
        const request = ++loading;
        try {
            const value = await (await api()).json();
            if (request === loading && dialog.open) setData(value);
        } catch (error) { status.textContent = error.message; }
    }
    window.openProtocolReview = async id => {
        if (dialog.open && dirty && !confirm('Отменить несохранённые изменения?')) return;
        jobId = id;
        setAudio(typeof queue !== 'undefined' ? queue.find(item => item.jobId === id)?.file : null);
        document.getElementById('review-audio-file').value = '';
        data = null;
        dirty = false;
        page = 0;
        content.replaceChildren();
        status.textContent = 'Загрузка расшифровки…';
        if (!dialog.open) dialog.showModal();
        await reload();
    };
    function close(event) {
        if (busy || (dirty && !confirm('Закрыть без сохранения изменений?'))) {
            if (event) event.preventDefault();
            return;
        }
        ++loading;
        setAudio(null);
        dialog.close();
    }
    document.getElementById('review-close').onclick = close;
    dialog.addEventListener('cancel', close);
    window.addEventListener('beforeunload', event => {
        if (dirty) { event.preventDefault(); event.returnValue = ''; }
    });

    async function save(reviewed = false) {
        if (busy) return false;
        busy = true;
        let message = '';
        dialog.querySelectorAll('button').forEach(el => { el.disabled = true; });
        try {
            const value = await (await api('', {
                method: 'PUT', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ ...edits, reviewed }),
            })).json();
            setData(value);
            message = reviewed ? 'Проверка подтверждена' : 'Изменения сохранены';
            if (typeof loadHistoryJobs === 'function') loadHistoryJobs();
            return true;
        } catch (error) {
            message = error.message;
            return false;
        } finally {
            busy = false;
            document.getElementById('review-close').disabled = false;
            render();
            status.textContent = message;
        }
    }
    function assignmentEditor(container, key, map, inherited) {
        const select = node('select');
        select.setAttribute('aria-label', inherited ? `Роль реплики ${key}` : `Роль голоса ${key}`);
        select.appendChild(new Option(inherited ? 'Как у голоса' : 'Выберите роль', ''));
        data.role_options.forEach(role => select.appendChild(new Option(role, role)));
        const name = node('input');
        name.placeholder = 'Имя, если установлено';
        name.maxLength = 150;
        name.setAttribute('aria-label', inherited ? `Имя в реплике ${key}` : `Имя участника ${key}`);
        select.value = map[key]?.role || '';
        name.value = map[key]?.name || '';
        const update = () => {
            if (select.value) map[key] = { role: select.value, name: name.value };
            else delete map[key];
            changed();
        };
        select.onchange = () => { update(); render(); };
        name.oninput = update;
        container.append(select, name);
    }
    function resultText(row) {
        if (Object.hasOwn(edits.manual_text, row.id)) return edits.manual_text[row.id];
        // Python offsets count Unicode code points; JS string.slice counts UTF-16.
        let text = Array.from(row.original);
        const accepted = data.corrections.filter(c => c.utterance_id === row.id && edits.decisions[c.id] === 'accepted');
        accepted.sort((a, b) => b.start - a.start).forEach(c => {
            text.splice(c.start, c.end - c.start, ...Array.from(c.replacement));
        });
        return text.join('');
    }
    function renderRow(row) {
        const article = node('article', undefined, 'review-utterance');
        const time = row.start_ms == null ? '' : ` · ${Math.floor(row.start_ms / 60000)}:${String(Math.floor(row.start_ms / 1000) % 60).padStart(2, '0')}`;
        const assignment = edits.utterances[row.id] || edits.speakers[row.speaker_id];
        article.appendChild(node('h4', `Спикер ${row.speaker_id}${assignment ? ' · ' + assignment.role : ''}${time}`));
        if (row.start_ms != null) article.appendChild(button('Прослушать реплику', async () => {
            if (!audioUrl) { status.textContent = 'Выберите исходную аудиозапись для сверки.'; return; }
            audio.currentTime = row.start_ms / 1000;
            try { await audio.play(); } catch (_) { status.textContent = 'Браузер не смог воспроизвести эту запись.'; }
        }));
        const columns = node('div', undefined, 'review-columns');
        const original = node('section');
        original.appendChild(node('strong', 'Исходная расшифровка'));
        const source = node('p', undefined, 'review-text');
        const sourceChars = Array.from(row.original);
        const corrections = data.corrections.filter(c => c.utterance_id === row.id).sort((a, b) => a.start - b.start);
        let end = 0;
        corrections.forEach(c => {
            source.append(document.createTextNode(sourceChars.slice(end, c.start).join('')));
            source.appendChild(node('mark', sourceChars.slice(c.start, c.end).join('')));
            end = c.end;
        });
        source.append(document.createTextNode(sourceChars.slice(end).join('')));
        original.appendChild(source);
        const proposed = node('section');
        proposed.appendChild(node('strong', 'Результат'));
        const output = node('textarea');
        output.setAttribute('aria-label', `Текст реплики ${row.id}`);
        output.rows = Math.min(16, Math.max(4, Math.ceil(row.original.length / 65)));
        output.value = resultText(row);
        output.oninput = () => { edits.manual_text[row.id] = output.value; changed(); };
        proposed.append(output, node('small', 'Текст можно исправить вручную. Ручная редакция заменяет предложения для этой реплики.'));
        proposed.appendChild(button('Отменить ручную редакцию', () => {
            delete edits.manual_text[row.id]; changed(); render();
        }));
        columns.append(original, proposed);
        article.appendChild(columns);
        const localRole = node('details');
        localRole.appendChild(node('summary', 'Роль и имя для отдельной реплики'));
        assignmentEditor(localRole, row.id, edits.utterances, true);
        article.appendChild(localRole);
        corrections.forEach(c => {
            const item = node('div', undefined, 'review-change');
            item.append(node('del', c.original), node('span', ' → '), node('ins', c.replacement), node('p', c.reason));
            if (c.sensitive) item.appendChild(node('p', 'Возможное изменение имени, числа или смысла. Сверьте с записью.'));
            const selected = edits.decisions[c.id];
            item.appendChild(node('small', selected === 'accepted' ? 'Принято' : selected === 'rejected' ? 'Отклонено' : 'Ожидает решения'));
            const actions = node('div', undefined, 'review-actions');
            for (const [label, decision] of [['Принять', 'accepted'], ['Отклонить', 'rejected'], ['Решить позже', null]]) {
                actions.appendChild(button(label, () => {
                    if (decision) edits.decisions[c.id] = decision; else delete edits.decisions[c.id];
                    changed(); render();
                }));
            }
            item.appendChild(actions);
            article.appendChild(item);
        });
        data.concerns.filter(c => c.utterance_id === row.id).forEach(c => {
            const label = node('label', undefined, 'review-concern');
            const check = node('input');
            check.type = 'checkbox';
            check.checked = edits.resolved_concerns.includes(c.id);
            check.onchange = () => {
                edits.resolved_concerns = edits.resolved_concerns.filter(id => id !== c.id);
                if (check.checked) edits.resolved_concerns.push(c.id);
                changed();
            };
            label.append(check, node('span', `Проверено: «${c.quote}» — ${c.reason}`));
            article.appendChild(label);
        });
        return article;
    }
    function render() {
        if (!data) return;
        const scroll = content.scrollTop;
        content.replaceChildren();
        status.textContent = dirty ? 'Есть несохранённые изменения' : data.error || (data.reviewed ? 'Проверка подтверждена' : 'Сверьте текст и роли с аудиозаписью');
        const controls = node('div', undefined, 'review-actions');
        controls.append(button('Сохранить', () => save()), button('Подтвердить проверку', () => save(true)), button('Обновить', reload));
        controls.appendChild(button('Скачать Word', async () => {
            if (dirty && !(await save())) return;
            try {
                const blob = await (await api('/docx')).blob();
                const url = URL.createObjectURL(blob);
                const link = node('a');
                link.href = url;
                link.download = data.reviewed ? 'Протокол.docx' : 'Черновик протокола.docx';
                link.click();
                setTimeout(() => URL.revokeObjectURL(url), 1000);
            } catch (error) { status.textContent = error.message; }
        }));
        if (data.stage === 'error') controls.appendChild(button('Повторить анализ', async () => {
            if (dirty && !(await save())) return;
            try { await api('/retry', { method: 'POST' }); await reload(); }
            catch (error) { status.textContent = error.message; }
        }));
        content.appendChild(controls);
        if (data.stage === 'processing') {
            content.appendChild(node('p', 'Анализ продолжается. Нажмите «Обновить», чтобы увидеть результат.'));
            controls.children[0].disabled = controls.children[1].disabled = true;
        }
        const form = node('fieldset');
        form.disabled = data.stage === 'processing';
        const fields = node('details');
        fields.appendChild(node('summary', 'Сведения для шапки и подписей'));
        fields.appendChild(node('p', 'Заполните только установленные сведения. Пустые поля не попадут в документ.'));
        const fieldGrid = node('div', undefined, 'review-fields');
        for (const [key, label] of Object.entries({ court: 'Суд', city: 'Место', hearing_date: 'Дата заседания', case_number: 'Номер дела', judge: 'Председательствующий', secretary: 'Секретарь' })) {
            const wrapper = node('label', label);
            const input = node('input');
            input.value = edits.fields[key] || '';
            input.maxLength = key === 'court' ? 250 : ['judge', 'secretary'].includes(key) ? 150 : 100;
            input.oninput = () => { edits.fields[key] = input.value; changed(); };
            wrapper.appendChild(input);
            fieldGrid.appendChild(wrapper);
        }
        fields.appendChild(fieldGrid);
        form.appendChild(fields);
        const speakers = node('details');
        speakers.open = true;
        speakers.appendChild(node('summary', 'Участники'));
        data.registry.forEach(entry => {
            const row = node('div', undefined, 'review-speaker');
            row.appendChild(node('strong', `Спикер ${entry.speaker_id}`));
            if (entry.status === 'conflict') row.appendChild(node('p', 'Предложения ролей противоречат друг другу. Нужна проверка.'));
            assignmentEditor(row, entry.speaker_id, edits.speakers, false);
            entry.candidates.forEach(candidate => {
                const suggestion = node('details');
                suggestion.appendChild(node('summary', `Предположение: ${candidate.role}${candidate.name ? ' — ' + candidate.name : ''}`));
                suggestion.appendChild(node('p', candidate.reason));
                candidate.evidence.forEach(e => suggestion.appendChild(node('blockquote', e.quote)));
                suggestion.appendChild(button('Подтвердить эту роль', () => {
                    edits.speakers[entry.speaker_id] = { role: candidate.role, name: candidate.name };
                    changed(); render();
                }));
                row.appendChild(suggestion);
            });
            speakers.appendChild(row);
        });
        form.appendChild(speakers);
        const navigation = node('div', undefined, 'review-actions');
        const previous = button('Назад', () => { page--; render(); content.scrollTop = 0; });
        const next = button('Далее', () => { page++; render(); content.scrollTop = 0; });
        previous.disabled = page === 0;
        next.disabled = (page + 1) * pageSize >= data.rows.length;
        navigation.append(previous, node('span', `Реплики ${page * pageSize + 1}–${Math.min((page + 1) * pageSize, data.rows.length)} из ${data.rows.length}`), next);
        form.appendChild(navigation);
        data.rows.slice(page * pageSize, (page + 1) * pageSize).forEach(row => form.appendChild(renderRow(row)));
        content.appendChild(form);
        const preview = node('details');
        preview.appendChild(node('summary', 'Предпросмотр сохранённого документа'));
        const paper = node('div', undefined, 'review-paper');
        data.blocks.forEach(block => paper.appendChild(node('p', block.kind === 'utterance' ? `${block.label}: ${block.text}` : block.text, `review-block-${block.kind}`)));
        preview.appendChild(paper);
        content.appendChild(preview);
        content.scrollTop = scroll;
    }
})();
