"""AI-reviewed activity groups over immutable bulletin publication identities.

Candidate selection never merges records: a reviewed partition with exact public
evidence authorizes each merge, including explicitly selected cross-title pairs.
"""
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import unicodedata


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _public_id(prefix, value):
    return prefix + hashlib.sha256(value.encode()).hexdigest()[:24]


def create_activity_schema(db):
    from nannanews import schema
    schema.execute_script(db, '''
        CREATE TABLE IF NOT EXISTS bulletin_activity_groups (
            id INTEGER PRIMARY KEY,
            canonical_item_id INTEGER NOT NULL REFERENCES bulletin_items(id),
            decision_sha256 TEXT NOT NULL UNIQUE,
            input_sha256 TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            reason TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS bulletin_activity_members (
            group_id INTEGER NOT NULL REFERENCES bulletin_activity_groups(id),
            item_id INTEGER NOT NULL REFERENCES bulletin_items(id),
            PRIMARY KEY(group_id,item_id));
        CREATE INDEX IF NOT EXISTS bulletin_activity_member_head
            ON bulletin_activity_members(item_id,group_id);
    ''')
    for table in ('bulletin_activity_groups', 'bulletin_activity_members'):
        for operation in ('UPDATE', 'DELETE'):
            db.execute(f'''CREATE TRIGGER IF NOT EXISTS {table}_immutable_{operation.lower()}
                BEFORE {operation} ON {table}
                BEGIN SELECT RAISE(ABORT,'bulletin activity decisions are immutable'); END''')


def activity_map(db):
    """Return every raw item's current canonical ID without modifying evidence."""
    result = {row[0]: row[0] for row in db.execute('SELECT id FROM bulletin_items')}
    if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='bulletin_activity_groups'").fetchone():
        for row in db.execute('''SELECT m.item_id,g.canonical_item_id
                FROM bulletin_activity_members m JOIN bulletin_activity_groups g ON g.id=m.group_id
                JOIN (SELECT item_id,max(group_id) group_id FROM bulletin_activity_members GROUP BY item_id) h
                ON h.item_id=m.item_id AND h.group_id=m.group_id'''):
            result[row[0]] = row[1]
    if any(canonical not in result or result[canonical] != canonical for canonical in result.values()):
        raise ValueError('Activity grouping has inconsistent canonical identities.')
    return result


def activity_candidates(db, *, item_ids=None):
    """Public evidence for same-title candidates or an explicit cross-title set."""
    rows = db.execute('''SELECT o.item_id,o.local_key,o.kind,o.title,o.body_text,o.raw_item_json,
            i.issue_date,d.source_sha256,b.identity_key,o.id
        FROM bulletin_occurrences o JOIN bulletin_documents d ON d.id=o.document_id
        JOIN bulletin_issues i ON i.id=d.issue_id JOIN bulletin_items b ON b.id=o.item_id
        ORDER BY i.issue_date DESC,o.id DESC''').fetchall()
    from nannanews import bulletin_source_repair
    urls = defaultdict(set)
    for row in db.execute('SELECT occurrence_id,source_url FROM ' + bulletin_source_repair.sources_table(db)):
        urls[row[0]].add(row[1])
    items, titles = {}, defaultdict(set)
    for row in rows:
        item_id = _public_id('item-', row['identity_key'])
        title = ''.join(unicodedata.normalize('NFC', row['title']).split())
        titles[title].add(item_id)
        item = items.setdefault(item_id, {'item_id': item_id, 'source_urls': set(), 'publications': {}})
        item['source_urls'].update(urls[row['id']])
        raw = json.loads(row['raw_item_json'])
        dates = [{key: claim[key] for key in ('kind', 'raw_text', 'normalized_date', 'resolution', 'evidence')}
                 for claim in raw['dates']]
        signature = _json([row['kind'], row['title'], row['body_text'], dates])
        publication = item['publications'].setdefault(signature, {
            'kind': row['kind'], 'title': row['title'], 'body_text': row['body_text'], 'dates': dates,
            'issue_dates': [], 'occurrence_ids': []})
        publication['issue_dates'].append(row['issue_date'])
        publication['occurrence_ids'].append(_public_id('occ-', row['source_sha256'] + ':' + row['local_key']))
    for item in items.values():
        item['source_urls'] = sorted(item['source_urls'])
        item['publications'] = list(item['publications'].values())
        for publication in item['publications']:
            publication['issue_dates'] = sorted(set(publication['issue_dates']), key=lambda value: value or '')
    if item_ids is not None:
        if (not isinstance(item_ids, (list, tuple)) or len(item_ids) < 2
                or any(not isinstance(item, str) or item not in items for item in item_ids)
                or len(set(item_ids)) != len(item_ids)):
            raise ValueError('Explicit activity selection requires distinct known public item IDs.')
        selected = sorted(item_ids)
        candidates = [{'key': 'explicit:' + hashlib.sha256(_json(selected).encode()).hexdigest(),
                       'matching_title': None, 'items': [items[item] for item in selected]}]
    else:
        candidates = [{'key': hashlib.sha256(title.encode()).hexdigest(), 'matching_title': title,
                       'items': [items[item_id] for item_id in sorted(ids)]}
                      for title, ids in sorted(titles.items()) if len(ids) > 1]
    snapshot = {'snapshot_sha256': hashlib.sha256(_json(candidates).encode()).hexdigest(),
                'candidates': candidates}
    if item_ids is not None:
        snapshot['explicit_item_ids'] = selected
    return snapshot


def _heads_digest(db):
    return hashlib.sha256(_json(sorted(activity_map(db).items())).encode()).hexdigest()


def plan_activity_merges(db, review_files):
    """Validate prepared AI decisions; dry-run has no model, network or writes."""
    snapshot = activity_candidates(db)
    candidates = {row['key']: row for row in snapshot['candidates']}
    raw_ids = {_public_id('item-', row['identity_key']): row['id']
               for row in db.execute('SELECT id,identity_key FROM bulletin_items')}
    current = activity_map(db)
    parent = dict(current)

    def root(item):
        while parent[item] != item:
            item = parent[item]
        return item

    reviewed, decisions, explicit_snapshots = set(), [], {}
    for filename in review_files:
        path = Path(filename)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError('Activity review must be a bounded regular JSON file.')
        value = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or set(value) not in (
                {'snapshot_sha256', 'decisions'}, {'snapshot_sha256', 'decisions', 'explicit_item_ids'})
                or not isinstance(value['decisions'], list)
                or ('explicit_item_ids' in value and not isinstance(value['explicit_item_ids'], list))):
            raise ValueError('Activity review must contain a snapshot-bound decision array.')
        review_snapshot = snapshot
        if 'explicit_item_ids' in value:
            review_snapshot = activity_candidates(db, item_ids=value['explicit_item_ids'])
            candidate = review_snapshot['candidates'][0]
            explicit_snapshots[candidate['key']] = {
                'item_ids': review_snapshot['explicit_item_ids'],
                'snapshot_sha256': review_snapshot['snapshot_sha256']}
        if value['snapshot_sha256'] != review_snapshot['snapshot_sha256']:
            raise ValueError('Activity review snapshot does not match the current public evidence.')
        file_candidates = {row['key']: row for row in review_snapshot['candidates']}
        for decision in value['decisions']:
            if (not isinstance(decision, dict) or set(decision) != {'key', 'groups', 'unmerged_item_ids', 'reason'}
                    or not isinstance(decision['key'], str) or decision['key'] not in file_candidates
                    or decision['key'] in reviewed or not isinstance(decision['groups'], list)
                    or not isinstance(decision['unmerged_item_ids'], list)
                    or not isinstance(decision['reason'], str) or not decision['reason'].strip()):
                raise ValueError('Activity review contains an invalid or duplicate candidate decision.')
            candidate = file_candidates[decision['key']]
            members = {item['item_id']: item for item in candidate['items']}
            covered = []
            for group in decision['groups']:
                if (not isinstance(group, dict) or set(group) != {'item_ids', 'reason', 'evidence'}
                        or not isinstance(group['item_ids'], list) or len(group['item_ids']) < 2
                        or any(not isinstance(item, str) or item not in members for item in group['item_ids'])
                        or len(set(group['item_ids'])) != len(group['item_ids'])
                        or not isinstance(group['reason'], str) or not group['reason'].strip()
                        or len(group['reason']) > 4000 or not isinstance(group['evidence'], list)):
                    raise ValueError('Activity merge requires distinct known members and a semantic explanation.')
                evidence_ids = []
                for evidence in group['evidence']:
                    if (not isinstance(evidence, dict) or set(evidence) != {'item_id', 'quote'}
                            or not isinstance(evidence['item_id'], str) or evidence['item_id'] not in group['item_ids']
                            or not isinstance(evidence['quote'], str) or not evidence['quote'].strip()
                            or not any(evidence['quote'] in publication[field]
                                       for publication in members[evidence['item_id']]['publications']
                                       for field in ('body_text', 'title'))):
                        raise ValueError('Activity merge evidence is absent from the claimed member.')
                    evidence_ids.append(evidence['item_id'])
                if set(evidence_ids) != set(group['item_ids']):
                    raise ValueError('Every merged activity member requires exact public evidence.')
                covered.extend(group['item_ids'])
                ids = [raw_ids[item] for item in group['item_ids']]
                canonical = min(root(item) for item in ids)
                for item in ids:
                    parent[root(item)] = canonical
                decisions.append({'candidate_key': decision['key'], 'item_ids': group['item_ids'],
                                  'reason': group['reason'], 'evidence': group['evidence'],
                                  'snapshot_sha256': review_snapshot['snapshot_sha256'],
                                  'reviewer': 'luna:' + path.stem})
            if any(not isinstance(item, str) for item in decision['unmerged_item_ids']):
                raise ValueError('Unmerged activity identities must be strings.')
            covered.extend(decision['unmerged_item_ids'])
            if len(covered) != len(set(covered)) or set(covered) != set(members):
                raise ValueError('Activity review must cover every candidate member exactly once.')
            reviewed.add(decision['key'])
    groups = defaultdict(list)
    for item in parent:
        groups[root(item)].append(item)
    changed = [{'canonical_item_id': canonical, 'item_ids': sorted(ids)}
               for canonical, ids in sorted(groups.items())
               if any(current[item] != canonical for item in ids)]
    return {'snapshot_sha256': snapshot['snapshot_sha256'], 'heads_sha256': _heads_digest(db),
            'explicit_snapshots': list(explicit_snapshots.values()), 'groups': changed, 'decisions': decisions,
            'summary': {'raw_items': len(current), 'activities_before': len(set(current.values())),
                        'activities_after': len(groups), 'reviewed_candidates': len(reviewed),
                        'unreviewed_candidates': len(candidates) + len(explicit_snapshots) - len(reviewed),
                        'merged_groups': len(changed), 'redundant_identities': len(set(current.values())) - len(groups)}}


def apply_activity_merges(db, plan):
    """Append atomically to a prepared component schema; preserve raw evidence IDs."""
    db.commit()
    with db:
        db.execute('BEGIN IMMEDIATE')
        if (activity_candidates(db)['snapshot_sha256'] != plan['snapshot_sha256']
                or _heads_digest(db) != plan['heads_sha256']
                or any(activity_candidates(db, item_ids=selection['item_ids'])['snapshot_sha256']
                       != selection['snapshot_sha256'] for selection in plan['explicit_snapshots'])):
            raise ValueError('Activity grouping changed; create a fresh dry-run plan.')
        by_public_id = {_public_id('item-', row['identity_key']): row['id']
                        for row in db.execute('SELECT id,identity_key FROM bulletin_items')}
        changed = 0
        for group in plan['groups']:
            members = set(group['item_ids'])
            evidence = [decision for decision in plan['decisions']
                        if any(by_public_id[item] in members for item in decision['item_ids'])]
            reason = '\n'.join(dict.fromkeys(decision['reason'] for decision in evidence))
            payload = dict(group, input_sha256=plan['snapshot_sha256'], decisions=evidence)
            digest = hashlib.sha256(_json(payload).encode()).hexdigest()
            cursor = db.execute('''INSERT OR IGNORE INTO bulletin_activity_groups
                (canonical_item_id,decision_sha256,input_sha256,reviewer,reason,evidence_json,created_at)
                VALUES(?,?,?,?,?,?,?)''', (group['canonical_item_id'], digest, plan['snapshot_sha256'],
                ','.join(sorted({decision['reviewer'] for decision in evidence})), reason,
                _json(evidence), datetime.now(timezone.utc).isoformat()))
            if cursor.rowcount:
                db.executemany('INSERT INTO bulletin_activity_members VALUES(?,?)',
                               [(cursor.lastrowid, item) for item in group['item_ids']])
                changed += 1
    return changed


def merge_activities(*, settings=None, review_files=(), apply=False, audit_dir=None):
    if settings is None:
        from nannanews import runtime
        settings = runtime.settings
    database = Path(settings.database_path).resolve(strict=True)
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        plan = plan_activity_merges(db, review_files)
    summary = dict(plan['summary'], applied=False, appended_groups=0, snapshot_sha256=plan['snapshot_sha256'])
    if audit_dir:
        output = Path(audit_dir)
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        os.chmod(output, 0o700)
        for name, value in (('plan.json', plan), ('summary.json', summary)):
            with (output / name).open('x', encoding='utf-8') as stream:
                os.chmod(stream.name, 0o600)
                stream.write(_json(value) + '\n')
    if apply:
        with closing(sqlite3.connect(database, timeout=30)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            summary['appended_groups'] = apply_activity_merges(db, plan)
        summary['applied'] = True
        if audit_dir:
            with (Path(audit_dir) / 'application.json').open('x', encoding='utf-8') as stream:
                os.chmod(stream.name, 0o600)
                stream.write(_json(summary) + '\n')
    return summary
