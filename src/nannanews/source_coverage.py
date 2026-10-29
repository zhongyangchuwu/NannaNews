"""Read-only, bounded original-article coverage; never an activity-recall estimate."""
from collections import Counter
import re
from urllib.parse import urlsplit

from nannanews import bulletin_activities, schema


DAILY_ACTORS = frozenset({'system:daily'})
PDF_BACKFILL_ACTOR = re.compile(
    r'(?:system:(?:pdf-coverage|pdf-network-recovery)-[0-9]{8}|audit:pdf-[0-9]{4}-[0-9]{2}-[0-9]{2})'
)


def _article_key(url):
    # This is a comparison-only alias, not acquisition/source/request identity.
    parsed = urlsplit(url)
    if (parsed.scheme in {'http', 'https'} and parsed.netloc == 'es.nju.edu.cn'
            and re.search(r'/c[0-9]+a[0-9]+/pagem?\.htm$', parsed.path)):
        return url.replace(parsed.path, re.sub(r'/pagem\.htm$', '/page.htm', parsed.path), 1)
    return url


def _aliases(url):
    key = _article_key(url)
    parsed = urlsplit(key)
    if (parsed.netloc == 'es.nju.edu.cn'
            and re.search(r'/c[0-9]+a[0-9]+/page\.htm$', parsed.path)
            and parsed.scheme in {'http', 'https'}):
        return {key, key.replace(parsed.path, parsed.path[:-8] + 'pagem.htm', 1)}
    return {url}


def _rows(db, sql, parameters=()):
    cursor = db.execute(sql, parameters)
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor]


def _matching_rows(db, sql, values):
    values = sorted(values)
    for start in range(0, len(values), 400):
        batch = values[start:start + 400]
        yield from _rows(db, sql.format(placeholders=','.join('?' for _ in batch)), batch)


def _actor_provenance(actor):
    if actor and PDF_BACKFILL_ACTOR.fullmatch(actor):
        return 'pdf_backfill'
    if actor in DAILY_ACTORS:
        return 'daily_capture'
    return 'other' if actor else 'unknown'


def source_coverage(db, *, issue_from=None, issue_to=None):
    """Compare effective bulletin URLs with queue and current saved-head metadata.

    The caller owns the connection/transaction. Only SELECT statements are issued;
    neither its row factory nor transaction state is changed. Numeric issue bounds
    are inclusive; no bounds selects the latest numbered retained issue.
    """
    for value in (issue_from, issue_to):
        if value is not None and (type(value) is not int or value < 1):
            raise ValueError('Issue bounds must be positive integers.')
    if issue_from is not None and issue_to is not None and issue_from > issue_to:
        raise ValueError('issue-from must not exceed issue-to.')
    schema.require_schema(db)
    default_latest = issue_from is None and issue_to is None
    if default_latest:
        latest = db.execute('SELECT max(issue_number) FROM bulletin_issues').fetchone()[0]
        issue_from = issue_to = latest
    where, parameters = ['i.issue_number IS NOT NULL'], []
    for operator, value in (('>=', issue_from), ('<=', issue_to)):
        if value is not None:
            where.append('i.issue_number ' + operator + ' ?')
            parameters.append(value)
    issues = _rows(db, 'SELECT i.id,i.issue_number FROM bulletin_issues i WHERE '
                   + ' AND '.join(where) + ' ORDER BY i.issue_number,i.id', parameters)
    occurrences = _rows(db, '''SELECT o.id,o.item_id,i.issue_number,s.source_url
        FROM bulletin_occurrences o JOIN bulletin_documents d ON d.id=o.document_id
        JOIN bulletin_issues i ON i.id=d.issue_id
        LEFT JOIN bulletin_effective_sources s ON s.occurrence_id=o.id
        WHERE ''' + ' AND '.join(where) + ' ORDER BY o.id,s.source_url', parameters)
    activities = bulletin_activities.activity_map(db)
    articles = {}
    all_occurrences, all_activities, source_occurrences = set(), set(), set()
    for row in occurrences:
        occurrence_id, activity_id = row['id'], activities[row['item_id']]
        all_occurrences.add(occurrence_id)
        all_activities.add(activity_id)
        if not row['source_url']:
            continue
        source_occurrences.add(occurrence_id)
        key = _article_key(row['source_url'])
        article = articles.setdefault(key, {
            'article_url': key, 'source_urls': set(), 'occurrence_ids': set(),
            'activity_ids': set(), 'issue_numbers': set(), 'queue': [], 'raw_heads': [],
            'entry_ids': set(), 'discovery_evidence': [],
        })
        article['source_urls'].add(row['source_url'])
        article['occurrence_ids'].add(occurrence_id)
        article['activity_ids'].add(activity_id)
        article['issue_numbers'].add(row['issue_number'])
    urls = {alias for key in articles for alias in _aliases(key)}
    queue_sql = '''SELECT l.id,l.source_id,l.url,l.state,l.eligible,l.first_seen,
        l.attempt_run,l.document_id,r.actor AS attempt_actor,r.mode AS attempt_mode
        FROM daily_links l LEFT JOIN daily_runs r ON r.id=l.attempt_run
        WHERE l.url IN ({placeholders}) ORDER BY l.id'''
    queue = list(_matching_rows(db, queue_sql, urls))
    queue_by_id = {row['id']: row for row in queue}
    for row in queue:
        articles[_article_key(row['url'])]['queue'].append(row)
    # The collector inserts first_seen using the run's exact started_at stamp.
    # attempt_run is deliberately NOT used as evidence of initial discovery.
    discovery_sql = '''SELECT l.id,r.id AS run_id,r.actor,r.mode,r.started_at
        FROM daily_links l JOIN daily_runs r ON r.started_at=l.first_seen
        JOIN daily_source_runs s ON s.run_id=r.id AND s.source_id=l.source_id
        WHERE l.url IN ({placeholders}) ORDER BY l.id,r.id'''
    for row in _matching_rows(db, discovery_sql, urls):
        link = queue_by_id[row.pop('id')]
        provenance = _actor_provenance(row['actor'])
        if provenance == 'daily_capture':
            provenance = 'independent_daily'
        articles[_article_key(link['url'])]['discovery_evidence'].append({
            'kind': provenance, 'basis': 'queue_first_seen_source_run',
            'queue_id': link['id'], **row,
        })
    head_sql = '''SELECT h.source_url,d.id AS document_id,d.source_id,d.captured_by,
        d.fetched_at,d.model_status FROM source_document_heads h
        JOIN source_documents d ON d.id=h.document_id
        WHERE h.source_url IN ({placeholders}) ORDER BY h.source_url'''
    documents = {}
    for row in _matching_rows(db, head_sql, urls):
        article = articles[_article_key(row['source_url'])]
        article['raw_heads'].append(row)
        documents[row['document_id']] = article
        article['discovery_evidence'].append({
            'kind': _actor_provenance(row['captured_by']), 'basis': 'raw_head_captured_by',
            'document_id': row['document_id'], 'actor': row['captured_by'],
        })
    entry_sql = '''SELECT r.document_id,r.entry_id FROM information_records r
        JOIN entries e ON e.id=r.entry_id WHERE r.document_id IN ({placeholders})
        ORDER BY r.entry_id'''
    for row in _matching_rows(db, entry_sql, documents):
        documents[row['document_id']]['entry_ids'].add(row['entry_id'])
    counts = Counter()
    for article in articles.values():
        kinds = {evidence['kind'] for evidence in article['discovery_evidence']}
        evidenced_queue = {evidence.get('queue_id') for evidence in article['discovery_evidence']}
        unknown_queue = any(row['id'] not in evidenced_queue for row in article['queue'])
        if 'independent_daily' in kinds:
            provenance = 'independent_daily'
        elif 'pdf_backfill' in kinds and not (kinds - {'pdf_backfill'}) and not unknown_queue:
            provenance = 'pdf_only'
        elif kinds == {'other'} and not unknown_queue:
            provenance = 'other_only'
        elif article['queue'] or article['raw_heads']:
            provenance = 'unknown'
        else:
            provenance = 'not_observed'
        article.update(discovery=provenance, queued=bool(article['queue']),
                       raw_saved=bool(article['raw_heads']),
                       extracted=any(head['model_status'] == 'completed' for head in article['raw_heads']),
                       has_entries=bool(article['entry_ids']))
        counts[provenance] += 1
        for stage in ('queued', 'raw_saved', 'extracted', 'has_entries'):
            counts[stage] += int(article[stage])
        for field in ('source_urls', 'occurrence_ids', 'activity_ids', 'issue_numbers', 'entry_ids'):
            article[field] = sorted(article[field])
        article['occurrence_count'] = len(article['occurrence_ids'])
        article['activity_count'] = len(article['activity_ids'])
    denominator = len(articles)
    return {
        'scope': {'issue_from': issue_from, 'issue_to': issue_to, 'default_latest_issue': default_latest,
                  'issue_numbers': sorted({row['issue_number'] for row in issues}),
                  'issue_count': len(issues), 'source_links': 'bulletin_effective_sources',
                  'saved_documents': 'current source_document_heads only'},
        'denominators': {'article_urls': denominator,
                         'effective_source_urls': sum(len(article['source_urls']) for article in articles.values()),
                         'bulletin_occurrences': len(all_occurrences), 'bulletin_activities': len(all_activities),
                         'occurrences_with_source': len(source_occurrences),
                         'occurrences_without_source': len(all_occurrences - source_occurrences)},
        'article_counts': {key: counts[key] for key in (
            'queued', 'raw_saved', 'extracted', 'has_entries', 'independent_daily',
            'pdf_only', 'other_only', 'unknown', 'not_observed')},
        'article_fractions': {key: {'numerator': counts[key], 'denominator': denominator,
                                   'fraction': counts[key] / denominator if denominator else None}
                              for key in ('queued', 'raw_saved', 'extracted', 'has_entries', 'independent_daily')},
        'limitations': [
            'Bounded effective original-source article coverage, not activity recall or campus-wide coverage.',
            'One article may describe multiple bulletin occurrences or activities; stages count articles once.',
            'Extraction means a current head has model_status=completed; entries are counted separately.',
            'Queue membership and latest attempt actor alone do not prove independent discovery.',
            'Other actors are not classified as daily; a daily capture alone does not prove listing discovery.',
            'ES page.htm/pagem.htm aliases are compared only on es.nju.edu.cn with identical article paths; stored identities are unchanged.',
        ],
        'articles': [articles[key] for key in sorted(articles)],
    }
