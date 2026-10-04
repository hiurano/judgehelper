import copy

import pytest

from browser_tests.test_recordings import ui as ui
from backend.services.protocol import (
    ROLES, corrected_utterances, document_blocks, normalize_source, proposals,
    role_registry,
)


@pytest.fixture
def review(ui):
    context, server = ui
    source = normalize_source({'utterances': [
        {'speaker': 'A', 'text': '🙂 Я видел видел <script>опасный текст</script>.'},
    ]})
    state = {'stage': 'ready', 'error': None, 'decisions': {}, 'speakers': {}, 'utterances': {},
             'fields': {}, 'manual_text': {}, 'resolved_concerns': [], 'reviewed': False,
             'batches': {'0': {'result': {'corrections': [{
                 'utterance_id': 'u000001', 'start': 10, 'end': 15, 'original': 'видел',
                 'replacement': 'заметил', 'reason': 'Проверить распознавание', 'category': 'recognition',
             }], 'roles': [], 'concerns': []}}}}
    server['jobs']['review'] = {'id': 'review', 'status': 'done', 'has_review': True, 'review_status': 'pending'}
    saved = {'revision': 0, 'payloads': [], 'conflict': False}

    def view():
        return {**state, 'revision': saved['revision'], 'role_options': ROLES, 'source': source,
                'rows': corrected_utterances(source, state), 'blocks': document_blocks(source, state),
                'corrections': proposals(state, 'corrections'), 'concerns': [], 'registry': role_registry(source, state)}

    def route(request):
        if request.request.method == 'PUT':
            if saved['conflict']:
                request.fulfill(status=409, json={'detail': 'Документ изменён в другой вкладке. Обновите его перед сохранением.'})
                return
            payload = request.request.post_data_json
            saved['payloads'].append(copy.deepcopy(payload))
            state.update({k: v for k, v in payload.items() if k != 'revision'})
            saved['revision'] += 1
        request.fulfill(json=view())

    context.route('**/jobs/review/review', route)
    page = context.new_page()
    page.goto('http://localhost/')
    page.get_by_role('button', name='Проверить', exact=True).click()
    page.get_by_role('dialog').wait_for()
    yield page, state, saved


def test_review_keeps_source_and_correct_unicode_offsets_and_persists_choices(review):
    page, state, saved = review
    output = page.get_by_role('textbox', name='Текст реплики u000001')
    assert output.input_value() == '🙂 Я видел видел <script>опасный текст</script>.'
    page.get_by_role('button', name='Принять', exact=True).click()
    assert output.input_value() == '🙂 Я видел заметил <script>опасный текст</script>.'
    assert page.locator('#review-content script').count() == 0
    assert 'видел видел' in page.locator('.review-text').inner_text()
    page.get_by_label('Роль голоса A', exact=True).select_option('Свидетель')
    page.get_by_role('button', name='Сохранить', exact=True).click()
    page.get_by_text('Изменения сохранены', exact=True).wait_for()
    assert saved['payloads'][0]['speakers']['A']['role'] == 'Свидетель'
    assert list(saved['payloads'][0]['decisions'].values()) == ['accepted']
    assert saved['payloads'][0]['manual_text'] == {}
    assert state['reviewed'] is False
    assert page.get_by_role('button', name='Назад', exact=True).is_disabled()
    page.get_by_role('button', name='Закрыть', exact=True).click()
    page.get_by_role('button', name='Проверить', exact=True).click()
    assert output.input_value() == '🙂 Я видел заметил <script>опасный текст</script>.'


def test_conflict_keeps_unsaved_manual_edits(review):
    page, state, saved = review
    saved['conflict'] = True
    output = page.get_by_role('textbox', name='Текст реплики u000001')
    output.fill('Ручная редакция, которую нельзя потерять.')
    page.get_by_role('button', name='Сохранить', exact=True).click()
    page.get_by_text('Документ изменён в другой вкладке. Обновите его перед сохранением.', exact=True).wait_for()
    assert output.input_value() == 'Ручная редакция, которую нельзя потерять.'
    assert state['manual_text'] == {}
    assert saved['payloads'] == []


def test_manual_edit_can_be_reverted_and_review_is_explicit(review):
    page, state, saved = review
    output = page.get_by_role('textbox', name='Текст реплики u000001')
    original = output.input_value()
    output.fill('Ручная редакция')
    page.get_by_role('button', name='Отменить ручную редакцию', exact=True).click()
    assert output.input_value() == original
    page.get_by_role('button', name='Отклонить', exact=True).click()
    page.get_by_label('Роль голоса A', exact=True).select_option('Роль не установлена')
    page.get_by_role('button', name='Подтвердить проверку', exact=True).click()
    page.get_by_text('Проверка подтверждена', exact=True).wait_for()
    assert state['reviewed'] is True
    assert saved['payloads'][-1]['reviewed'] is True
