"""Literal evidence and version binding, without network or paid model calls."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3


import pytest



from nannanews import acquisition
from nannanews import bulletin_date_repair as repair
from nannanews import content_rules
from nannanews import static_export
from test_acquisition import document, item
from test_bulletin_date_repair import (
    SOURCE_TEXT, SOURCE_URL, claim, database, prepared, source_review, source_snapshot,
)
from test_static_export import evidence, _load




def test_acquisition_keeps_rejected_structural_answer(monkeypatch):
    output = {'items': [dict(item(), title='')]}
    original = deepcopy(output)
    monkeypatch.setattr(acquisition, '_model_config', lambda: object())
    def request(instructions, source_data, media):
        return output, 'offline-test-model'
    monkeypatch.setattr(acquisition, 'request_model_json', request)
    source = document(SOURCE_TEXT)
    with pytest.raises(acquisition.AcquisitionError) as error:
        acquisition.extract_document(source)
    assert error.value.code == 'model_output_invalid'
    assert source['model_output'] == original == output

@pytest.mark.parametrize(('source', 'quote'), [
    ('Date: 2025-11-27', ' Date: 2025-11-27 '),
    ('Date:\n2025-11-27', 'Date: 2025-11-27'),
    ('Cafe\u0301: 2025-11-27', 'Café: 2025-11-27'),
])
def test_new_primary_citations_and_saved_evidence_reject_normalization(source, quote):
    captured = source_snapshot(text=source, quotes=[quote])
    with pytest.raises(ValueError, match='fabricated text quotation'):
        repair._validate_source_review(source_review(sources=[captured]))
    captured['images'] = []
    with pytest.raises(ValueError, match='not present'):
        repair._validate_model_citations([{'url': SOURCE_URL,
            'quotes': [{'text': quote, 'image_index': None}]}], [captured])


@pytest.mark.parametrize(('occurrence', 'raw'), [
    ({'title': '日期：', 'body_text': '2025-11-27'}, '日期：\n2025-11-27'),
    ({'title': '日期：', 'body_text': '2025-11-27'}, '日期：2025-11-27'),
    ({'body_text': '日期：\n2025-11-27'}, '日期： 2025-11-27'),
    ({'body_text': 'Cafe\u0301: 2025-11-27'}, 'Café: 2025-11-27'),
    ({'body_text': '2025-', 'related_occurrences': [{'body_text': '11-27'}]}, '2025-11-27'),
])
def test_historical_raw_quote_cannot_cross_fields_or_normalize(occurrence, raw):
    with pytest.raises(ValueError, match='absent'):
        repair._validate_review_claim_sources([claim(raw=raw)], occurrence,
            source_review(status='secondary_only', sources=[]))


def test_contextual_claim_uses_literal_quote_and_separate_chinese_basis(database, tmp_path):
    raw = '活动于11月27日举行。'
    with database:
        database.execute('UPDATE bulletin_occurrences SET body_text=? WHERE id=1', (raw,))
    rows, originals, _ = repair.date_snapshot(database)
    contextual = dict(claim(raw=raw), resolution='contextual',
        evidence='同一活动正文明确11月27日；以issue_date元数据2026-10-13提供年份语境。',
        normalized_date='2026-11-27')
    value = prepared(rows[0], originals[1], claims=[contextual],
        review=source_review(status='secondary_only', sources=[]))
    path = tmp_path / 'review.json'
    path.write_text(json.dumps([value]), encoding='utf-8')
    result = repair.plan_repairs(database, review_files=[path])
    assert result['entries'][0]['claims'] == [contextual]
    assert contextual['evidence'] not in raw


@pytest.mark.parametrize('change', ['unbound', 'old_rules', 'wrong_input', 'wrong_context'])
def test_every_new_prepared_review_requires_current_rules_input_and_context(database, tmp_path, change):
    rows, originals, _ = repair.date_snapshot(database)
    value = prepared(rows[0], originals[1])
    if change == 'unbound':
        value.pop('input_sha256')
        value.pop('related_context_sha256')
    elif change == 'old_rules':
        value['rules_version'] = 'luna-source-first-v2'
    elif change == 'wrong_input':
        value['input_sha256'] = '0' * 64
    else:
        value['related_context_sha256'] = '0' * 64
    path = tmp_path / 'review.json'
    path.write_text(json.dumps([value]), encoding='utf-8')
    with pytest.raises(ValueError):
        repair.plan_repairs(database, review_files=[path])
    assert database.execute('SELECT count(*) FROM bulletin_date_revisions').fetchone()[0] == 0


def test_mismatched_source_cannot_supply_a_primary_date():
    with pytest.raises(ValueError, match='absent'):
        repair._validate_review_claim_sources([claim()], {'body_text': '另一个活动，日期待定'},
            source_review(status='source_mismatch'))


def test_legacy_nonliteral_receipt_is_metadata_only_without_mutating_it():
    review = source_review(sources=[source_snapshot(text='Date:\n2025-11-27', quotes=['Date: 2025-11-27'])])
    original = deepcopy(review)
    projected = repair.public_source_review(review, rules_version='luna-source-first-v2')
    assert review == original
    assert projected['status'] == 'secondary_only'
    assert projected['sources'][0]['quotes'] == []
    assert projected['sources'][0]['sha256'] == review['sources'][0]['raw_response_digest']
    assert '尚未' in projected['reason']
    assert repair.public_source_review(review, rules_version=repair.RULES_VERSION)['status'] == 'secondary_only'


def test_readonly_export_preserves_legacy_nonliteral_record_without_current_verification(evidence, tmp_path):
    review = source_review(sources=[source_snapshot(text='Date:\n2025-11-27', quotes=['Date: 2025-11-27'])])
    receipt = json.dumps(review, ensure_ascii=False)
    with closing(sqlite3.connect(evidence.database_path)) as db, db:
        # Simulate the immutable pre-v3 receipt; new admission must not accept it.
        db.execute('''INSERT INTO bulletin_date_revisions
            (id,occurrence_id,input_sha256,revision_sha256,rules_version,origin,reviewer,status,
             reason,source_review_json,created_at) VALUES(1,100,?,?,?,'luna_review','luna:old',
             'resolved','旧规则复核',?,'2026-10-14')''',
            ('0' * 64, '1' * 64, 'luna-source-first-v2', receipt))
        db.execute('''INSERT INTO bulletin_date_revision_claims
            (revision_id,kind,raw_text,normalized_date,resolution,evidence,locator_json)
            VALUES(1,'event','Date: 2025-11-27','2025-11-27','explicit','历史日期判断','null')''')
    before = evidence.database_path.read_bytes()
    output = tmp_path / 'public-legacy'
    static_export.export_static(output, settings=evidence)
    assert evidence.database_path.read_bytes() == before
    manifest, _, _, _, details = _load(output)
    assert manifest['content_rules_version'] == repair.RULES_VERSION
    occurrences = [row for detail in details.values() for row in detail['occurrences']]
    legacy = [row for row in occurrences if row['date_review']][0]
    assert legacy['date_review']['rules_version'] == 'legacy-content-evidence'
    assert legacy['date_review']['source_review']['status'] == 'secondary_only'
    assert legacy['date_review']['source_review']['sources'][0]['quotes'] == []
    assert any(value['normalized_date'] == '2025-11-27' for value in legacy['dates'])
    with closing(sqlite3.connect(evidence.database_path)) as db:
        assert db.execute('SELECT source_review_json FROM bulletin_date_revisions WHERE id=1').fetchone()[0] == receipt


def test_acquisition_returns_many_distinct_items_without_evidence(monkeypatch):
    values = []
    for number in range(1, 22):
        title = f'模型选择活动第{number}场'
        values.append(item(title=title, due=f'2026-10-{number:02d}',
                           quote=f'模型选择活动第{number}场的完整说明。'))
    source = document('原文没有逐条重复模型选择的标题、日期或说明。')
    monkeypatch.setattr(acquisition, '_model_config', lambda: object())
    def request(instructions, source_data, media):
        return {'items': deepcopy(values)}, 'offline-test-model'
    monkeypatch.setattr(acquisition, 'request_model_json', request)
    drafts = acquisition.extract_document(source)
    assert len(drafts) == 21
    assert [draft['title'] for draft in drafts] == [value['title'] for value in values]
    assert [draft['event_date'] for draft in drafts] == [value['event_date'] for value in values]
    assert source['model_output']['items'] == values
