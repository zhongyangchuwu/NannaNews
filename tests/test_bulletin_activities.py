"""Reviewed identity merges keep publication evidence and session boundaries."""
import json
from pathlib import Path
import sqlite3


import pytest


from nannanews import bulletin_activities as activities


@pytest.fixture
def database():
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.executescript('''
        CREATE TABLE bulletin_items(id INTEGER PRIMARY KEY,identity_key TEXT);
        CREATE TABLE bulletin_issues(id INTEGER PRIMARY KEY,issue_date TEXT);
        CREATE TABLE bulletin_documents(id INTEGER PRIMARY KEY,issue_id INTEGER,source_sha256 TEXT);
        CREATE TABLE bulletin_occurrences(id INTEGER PRIMARY KEY,item_id INTEGER,document_id INTEGER,
            local_key TEXT,kind TEXT,title TEXT,body_text TEXT,raw_item_json TEXT);
        CREATE TABLE bulletin_sources(occurrence_id INTEGER,source_url TEXT);
    ''')
    db.executemany('INSERT INTO bulletin_issues VALUES(?,?)', [(1, '2026-10-12'), (2, '2026-10-13')])
    db.executemany('INSERT INTO bulletin_documents VALUES(?,?,?)', [(1, 1, 'a' * 64), (2, 2, 'b' * 64)])
    rows = [
        (1, 1, 1, 'summary', '校园讲座\n甲', '活动甲，截止10-20，刊载10-12。'),
        (2, 2, 1, 'detail', '校园讲座甲', '活动甲，时间10月20日10:00，地点A楼。'),
        (3, 3, 1, 'summary', '校园讲座甲', '另一场活动甲，时间10月22日10:00，地点B楼。'),
        (4, 4, 1, 'summary', '比赛乙', '比赛乙，报名截止10月26日。'),
        (5, 5, 2, 'detail', '比赛乙', '比赛乙，报名截止10月26日。'),
        (6, 1, 2, 'summary', '校园讲座\n甲', '活动甲，截止10-20，刊载10-12。'),
    ]
    for item_id in range(1, 6):
        db.execute('INSERT INTO bulletin_items VALUES(?,?)', (item_id, 'raw:' + str(item_id)))
    for occurrence_id, item_id, document_id, kind, title, body in rows:
        db.execute('INSERT INTO bulletin_occurrences VALUES(?,?,?,?,?,?,?,?)',
                   (occurrence_id, item_id, document_id, str(occurrence_id), kind, title, body,
                    json.dumps({'dates': []}, ensure_ascii=False)))
        db.execute('INSERT INTO bulletin_sources VALUES(?,?)',
                   (occurrence_id, 'https://public.example.edu/activity/' + ('a' if item_id < 4 else 'b')))
    db.commit()
    activities.create_activity_schema(db)
    yield db
    db.close()


def review_file(db, tmp_path):
    snapshot = activities.activity_candidates(db)
    decisions = []
    for candidate in snapshot['candidates']:
        ids = [item['item_id'] for item in candidate['items']]
        if len(ids) == 3:
            first = activities._public_id('item-', 'raw:1')
            second = activities._public_id('item-', 'raw:2')
            merge = [first, second]
        else:
            merge = ids
        decisions.append({'key': candidate['key'], 'groups': [{
            'item_ids': merge, 'reason': '同一来源中相同场次的清单与详情；不同场次另存。',
            'evidence': [{'item_id': item['item_id'], 'quote': item['publications'][0]['body_text']}
                         for item in candidate['items'] if item['item_id'] in merge]}],
            'unmerged_item_ids': [item for item in ids if item not in merge],
            'reason': '10月22日另有时间和地点，保留为独立场次。'})
    path = tmp_path / 'review.json'
    path.write_text(json.dumps({'snapshot_sha256': snapshot['snapshot_sha256'], 'decisions': decisions},
                               ensure_ascii=False), encoding='utf-8')
    return path


def test_confirmed_groups_retain_all_publications_and_distinct_sessions(database, tmp_path):
    before = [tuple(row) for row in database.execute('SELECT * FROM bulletin_occurrences ORDER BY id')]
    review = review_file(database, tmp_path)
    plan = activities.plan_activity_merges(database, [review])
    assert plan['summary']['activities_before'] == 5
    assert plan['summary']['activities_after'] == 3
    assert activities.apply_activity_merges(database, plan) == 2
    assert activities.activity_map(database) == {1: 1, 2: 1, 3: 3, 4: 4, 5: 4}
    assert [tuple(row) for row in database.execute('SELECT * FROM bulletin_occurrences ORDER BY id')] == before
    repeat = activities.plan_activity_merges(database, [review])
    assert repeat['summary']['activities_after'] == 3
    assert activities.apply_activity_merges(database, repeat) == 0
    with pytest.raises(sqlite3.IntegrityError, match='immutable'):
        database.execute("UPDATE bulletin_activity_groups SET reason='rewrite'")


@pytest.mark.parametrize('corruption', ['quote', 'missing_member', 'duplicate_member', 'unknown_member'])
def test_invented_or_incomplete_review_cannot_merge(database, tmp_path, corruption):
    path = review_file(database, tmp_path)
    value = json.loads(path.read_text())
    decision = value['decisions'][0]
    if corruption == 'quote':
        decision['groups'][0]['evidence'][0]['quote'] = '原文并没有这句话。'
    elif corruption == 'missing_member':
        decision['groups'][0]['evidence'].pop()
    elif corruption == 'duplicate_member':
        decision['unmerged_item_ids'].append(decision['groups'][0]['item_ids'][0])
    else:
        decision['unmerged_item_ids'].append('item-' + 'f' * 24)
    path.write_text(json.dumps(value), encoding='utf-8')
    with pytest.raises(ValueError):
        activities.plan_activity_merges(database, [path])
    assert activities.activity_map(database) == {item: item for item in range(1, 6)}


def test_changed_raw_snapshot_or_group_heads_reject_stale_plan(database, tmp_path):
    path = review_file(database, tmp_path)
    plan = activities.plan_activity_merges(database, [path])
    activities.apply_activity_merges(database, plan)
    with pytest.raises(ValueError, match='changed'):
        activities.apply_activity_merges(database, plan)
    fresh = activities.plan_activity_merges(database, [path])
    database.execute("UPDATE bulletin_occurrences SET body_text='changed evidence' WHERE id=3")
    database.commit()
    with pytest.raises(ValueError, match='changed'):
        activities.apply_activity_merges(database, fresh)


def test_member_failure_rolls_back_every_group(database, tmp_path):
    path = review_file(database, tmp_path)
    plan = activities.plan_activity_merges(database, [path])
    activities.create_activity_schema(database)
    database.execute('''CREATE TRIGGER reject_later_member BEFORE INSERT ON bulletin_activity_members
        WHEN NEW.item_id=5 BEGIN SELECT RAISE(ABORT,'forced member failure'); END''')
    database.commit()
    with pytest.raises(sqlite3.IntegrityError, match='forced member failure'):
        activities.apply_activity_merges(database, plan)
    assert activities.activity_map(database) == {item: item for item in range(1, 6)}
    assert database.execute('SELECT count(*) FROM bulletin_activity_groups').fetchone()[0] == 0


def cross_title_review(db, tmp_path):
    db.execute("UPDATE bulletin_occurrences SET title='校园讲座甲｜详细通知' WHERE item_id=2")
    db.commit()
    ids = [activities._public_id('item-', 'raw:' + str(item)) for item in (1, 2)]
    snapshot = activities.activity_candidates(db, item_ids=ids)
    candidate = snapshot['candidates'][0]
    decision = {'key': candidate['key'], 'groups': [{
        'item_ids': ids, 'reason': '10月20日活动甲的摘要与详细通知，非10月22日另一场。',
        'evidence': [{'item_id': item['item_id'], 'quote': item['publications'][0]['body_text']}
                     for item in candidate['items']]}], 'unmerged_item_ids': [],
        'reason': '明确逐条核对不同标题下同一场次的完整正文。'}
    path = tmp_path / 'cross-title.json'
    path.write_text(json.dumps({'snapshot_sha256': snapshot['snapshot_sha256'],
        'explicit_item_ids': snapshot['explicit_item_ids'], 'decisions': [decision]}, ensure_ascii=False))
    return path


def test_explicit_cross_title_merge_coexists_with_same_title_review(database, tmp_path):
    path = cross_title_review(database, tmp_path)
    snapshot = activities.activity_candidates(database)
    candidate = next(row for row in snapshot['candidates'] if row['matching_title'] == '比赛乙')
    ids = [item['item_id'] for item in candidate['items']]
    decision = {'key': candidate['key'], 'groups': [{'item_ids': ids, 'reason': '同一报名活动。',
        'evidence': [{'item_id': item['item_id'], 'quote': item['publications'][0]['body_text']}
                     for item in candidate['items']]}], 'unmerged_item_ids': [], 'reason': '摘要与详情。'}
    legacy = tmp_path / 'same-title.json'
    legacy.write_text(json.dumps({'snapshot_sha256': snapshot['snapshot_sha256'], 'decisions': [decision]}))
    before = [tuple(row) for row in database.execute('SELECT * FROM bulletin_occurrences ORDER BY id')]
    plan = activities.plan_activity_merges(database, [legacy, path])
    assert activities.apply_activity_merges(database, plan) == 2
    assert activities.activity_map(database) == {1: 1, 2: 1, 3: 3, 4: 4, 5: 4}
    assert [tuple(row) for row in database.execute('SELECT * FROM bulletin_occurrences ORDER BY id')] == before
    assert activities.apply_activity_merges(database, activities.plan_activity_merges(database, [legacy, path])) == 0


@pytest.mark.parametrize('boundary', ['review', 'apply'])
def test_changed_singleton_evidence_rejects_explicit_review_or_plan(database, tmp_path, boundary):
    path = cross_title_review(database, tmp_path)
    plan = activities.plan_activity_merges(database, [path])
    database.execute("UPDATE bulletin_occurrences SET body_text='改期为10月23日，地点C楼。' WHERE item_id=2")
    database.commit()
    with pytest.raises(ValueError, match='snapshot|changed'):
        if boundary == 'review':
            activities.plan_activity_merges(database, [path])
        else:
            activities.apply_activity_merges(database, plan)
    assert activities.activity_map(database) == {item: item for item in range(1, 6)}


def test_explicit_review_cannot_substitute_unselected_member(database, tmp_path):
    path = cross_title_review(database, tmp_path)
    value = json.loads(path.read_text())
    value['decisions'][0]['groups'][0]['item_ids'][0] = activities._public_id('item-', 'raw:3')
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        activities.plan_activity_merges(database, [path])
    assert activities.activity_map(database) == {item: item for item in range(1, 6)}
