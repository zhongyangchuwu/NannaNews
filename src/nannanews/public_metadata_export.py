"""Build a deterministic, whitelist-only reader facts snapshot from public pack files."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlsplit

from nannanews.static_export import GENERATOR, _json, _no_symlinks, _public_url

_SCHEMA_VERSION = 1
_HASH = re.compile(r'[0-9a-f]{64}\Z')
_ITEM = re.compile(r'item-[0-9a-f]{24}\Z')
_OCCURRENCE = re.compile(r'occ-[0-9a-f]{24}\Z')
_ISSUE = re.compile(r'issue-[0-9a-f]{24}\Z')
_SOURCE = re.compile(r'source-[0-9a-f]{24}\Z')
_DATE = re.compile(r'\d{4}-\d{2}-\d{2}\Z')
_ACTIVITY_FACTS = ('venue', 'audience', 'participation', 'time')
_CONTENT_STATUSES = frozenset({'verified', 'human_verified', 'conflict', 'unconfirmed',
                               'unreviewed', 'legacy', 'source_corrected', 'needs_review'})
_MAX_FACTS_BYTES = 128 * 1024 * 1024


class PublicFactsError(ValueError):
    """A public facts source or destination does not satisfy its safe contract."""


def _valid_timestamp(value):
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _url_list(value):
    if not isinstance(value, list) or len(value) > 100:
        raise PublicFactsError('Public facts contain an invalid source URL list.')
    result = []
    for url in value:
        if _public_url(url) is None:
            raise PublicFactsError('Public facts contain a non-public source URL.')
        if url not in result:
            result.append(url)
    return result


def _activity_facts(value):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != set(_ACTIVITY_FACTS):
        raise PublicFactsError('Public facts contain invalid reviewed activity fields.')
    if any(field is not None and (not isinstance(field, str) or not field.strip()) for field in value.values()):
        raise PublicFactsError('Public facts contain invalid reviewed activity fields.')
    return {field: value[field] for field in _ACTIVITY_FACTS}


def _manual_stamp(value):
    if value is None:
        return None
    expected = {'revision', 'status', 'reviewed_at', 'reason'}
    if (not isinstance(value, dict) or set(value) != expected
            or not isinstance(value['revision'], str) or not _HASH.fullmatch(value['revision'])
            or value['status'] not in {'verified', 'needs_review'}
            or not _valid_timestamp(value['reviewed_at'])
            or not isinstance(value['reason'], str) or not value['reason'].strip()):
        raise PublicFactsError('Public facts contain an invalid published review stamp.')
    return {key: value[key] for key in ('revision', 'status', 'reviewed_at', 'reason')}


def _date_facts(values):
    if not isinstance(values, list) or len(values) > 100:
        raise PublicFactsError('Public facts contain an invalid date list.')
    result = []
    for value in values:
        if not isinstance(value, dict):
            raise PublicFactsError('Public facts contain an invalid date entry.')
        kind, day, resolution = value.get('kind'), value.get('normalized_date'), value.get('resolution')
        if (not isinstance(kind, str) or not kind or (day is not None and
                (not isinstance(day, str) or not _DATE.fullmatch(day)))
                or resolution not in {'explicit', 'contextual', 'year_unknown', 'invalid', 'ambiguous'}):
            raise PublicFactsError('Public facts contain an invalid date entry.')
        row = {'kind': kind, 'normalized_date': day, 'resolution': resolution}
        if 'literal_source' in value:
            source = value['literal_source']
            if source not in {'primary', 'bulletin', 'manual'}:
                raise PublicFactsError('Public facts contain an invalid date source marker.')
            row['literal_source'] = source
        result.append(row)
    return result


def build_public_facts(catalog, issues):
    """Project public catalog/issue whitelists into a compact deterministic file."""
    if (not isinstance(catalog, dict) or not isinstance(catalog.get('items'), list)
            or not isinstance(catalog.get('occurrences'), list) or not isinstance(issues, list)):
        raise PublicFactsError('Public facts require a valid catalog and issue list.')

    issue_ids, documents_by_issue, public_issues = set(), {}, []
    for issue in issues:
        if not isinstance(issue, dict) or not isinstance(issue.get('id'), str) or not _ISSUE.fullmatch(issue['id']) or issue['id'] in issue_ids:
            raise PublicFactsError('Public facts contain a duplicate or invalid issue.')
        issue_ids.add(issue['id'])
        documents = issue.get('documents')
        if not isinstance(documents, list):
            raise PublicFactsError('Public facts contain an invalid issue document list.')
        compact_documents, document_ids = [], set()
        for document in documents:
            if (not isinstance(document, dict) or not isinstance(document.get('id'), str)
                    or not _HASH.fullmatch(document['id']) or document['id'] in document_ids
                    or document.get('format') not in {'html', 'pdf'}
                    or not isinstance(document.get('filename'), str)):
                raise PublicFactsError('Public facts contain an invalid public document.')
            document_ids.add(document['id'])
            urls = _url_list(document.get('source_urls', []))
            warnings = document.get('warnings', [])
            if not isinstance(warnings, list) or any(not isinstance(warning, str) for warning in warnings):
                raise PublicFactsError('Public facts contain invalid public document warnings.')
            compact_documents.append({
                'id': document['id'], 'format': document['format'], 'filename': document['filename'],
                'claimed_issue_date': document.get('claimed_issue_date'), 'issue_date': document.get('issue_date'),
                'pdf_path': None, 'source_urls': urls, 'warnings': sorted(set(warnings)),
            })
        documents_by_issue[issue['id']] = document_ids
        number, count = issue.get('issue_number'), issue.get('occurrence_count')
        if (number is not None and (type(number) is not int)
                or type(count) is not int or count < 0
                or not isinstance(issue.get('title'), str)
                or (issue.get('issue_date') is not None and not isinstance(issue['issue_date'], str))):
            raise PublicFactsError('Public facts contain invalid issue metadata.')
        public_issues.append({
            'id': issue['id'], 'title': issue['title'], 'issue_date': issue['issue_date'],
            'issue_number': number, 'occurrence_count': count,
            'documents': sorted(compact_documents, key=lambda row: row['id']),
        })

    items_by_id = {}
    for item in catalog['items']:
        if (not isinstance(item, dict) or not isinstance(item.get('id'), str)
                or not _ITEM.fullmatch(item['id']) or item['id'] in items_by_id
                or not isinstance(item.get('latest_occurrence_id'), str)
                or not _OCCURRENCE.fullmatch(item['latest_occurrence_id'])):
            raise PublicFactsError('Public facts contain a duplicate or invalid activity.')
        aliases = item.get('aliases', [])
        if not isinstance(aliases, list) or any(not isinstance(alias, str) or not _ITEM.fullmatch(alias) for alias in aliases):
            raise PublicFactsError('Public facts contain invalid public activity aliases.')
        items_by_id[item['id']] = {
            'id': item['id'], 'latest_occurrence_id': item['latest_occurrence_id'],
            'aliases': sorted(set(aliases)), 'occurrence_count': item.get('occurrence_count'), 'occurrences': [],
        }

    occurrence_ids = set()
    for occurrence in catalog['occurrences']:
        if not isinstance(occurrence, dict):
            raise PublicFactsError('Public facts contain an invalid occurrence.')
        identity, item_id, kind = occurrence.get('id'), occurrence.get('item_id'), occurrence.get('kind')
        if (not isinstance(identity, str) or not _OCCURRENCE.fullmatch(identity) or identity in occurrence_ids
                or item_id not in items_by_id or kind not in {'summary', 'detail', 'notice', 'standalone'}
                or not isinstance(occurrence.get('title'), str)
                or not isinstance(occurrence.get('preview'), str)):
            raise PublicFactsError('Public facts contain an invalid or duplicate occurrence.')
        issue_id, document_id = occurrence.get('issue_id'), occurrence.get('document_id')
        activity_id, source_id = occurrence.get('activity_id'), occurrence.get('source_id')
        if kind == 'standalone':
            if (issue_id is not None or document_id is not None or activity_id != item_id
                    or not isinstance(source_id, str) or not _SOURCE.fullmatch(source_id)):
                raise PublicFactsError('Standalone public facts require stable registry identities and no issue/document.')
        elif (not isinstance(issue_id, str) or issue_id not in issue_ids
                or not isinstance(document_id, str) or not _HASH.fullmatch(document_id)
                or document_id not in documents_by_issue[issue_id]):
            raise PublicFactsError('Issue-linked public facts must retain their actual issue/document references.')
        if activity_id is not None and activity_id != item_id:
            raise PublicFactsError('Public facts contain an inconsistent activity identity.')
        if source_id is not None and (not isinstance(source_id, str) or not _SOURCE.fullmatch(source_id)):
            raise PublicFactsError('Public facts contain an invalid source identity.')
        urls = _url_list(occurrence.get('source_urls', []))
        hosts = sorted({urlsplit(url).hostname.lower() for url in urls})
        colleges = occurrence.get('colleges', [])
        if not isinstance(colleges, list) or any(not isinstance(name, str) for name in colleges):
            raise PublicFactsError('Public facts contain invalid activity labels.')
        content_status = occurrence.get('content_status')
        if content_status is not None and content_status not in _CONTENT_STATUSES:
            raise PublicFactsError('Public facts contain an invalid public content status.')
        category, issue_date, expiry_date = occurrence.get('category'), occurrence.get('issue_date'), occurrence.get('expiry_date')
        if category is not None and not isinstance(category, str):
            raise PublicFactsError('Public facts contain an invalid category.')
        if issue_date is not None and not isinstance(issue_date, str):
            raise PublicFactsError('Public facts contain an invalid issue date.')
        if expiry_date is not None and not isinstance(expiry_date, str):
            raise PublicFactsError('Public facts contain an invalid expiry date.')
        image_count = occurrence.get('image_count', 0)
        if type(image_count) is not int or image_count < 0:
            raise PublicFactsError('Public facts contain an invalid image count.')
        facts = _activity_facts(occurrence.get('activity_facts'))
        manual_review = _manual_stamp(occurrence.get('manual_review'))
        row = {
            'id': identity, 'item_id': item_id, 'issue_id': issue_id, 'document_id': document_id,
            'kind': kind, 'title': occurrence['title'], 'category': category,
            'preview': occurrence['preview'], 'issue_date': issue_date,
            'source_urls': urls, 'source_hosts': hosts, 'colleges': sorted(set(colleges)),
            'dates': _date_facts(occurrence.get('dates', [])), 'expiry_date': expiry_date,
            'image_count': image_count, 'content_status': content_status,
        }
        if facts is not None:
            row['activity_facts'] = facts
        if manual_review is not None:
            row['manual_review'] = manual_review
        if activity_id is not None:
            row['activity_id'] = activity_id
        if source_id is not None:
            row['source_id'] = source_id
        items_by_id[item_id]['occurrences'].append(row)
        occurrence_ids.add(identity)

    for item in items_by_id.values():
        item['occurrences'].sort(key=lambda row: row['id'])
        if (type(item['occurrence_count']) is not int or item['occurrence_count'] != len(item['occurrences'])
                or item['latest_occurrence_id'] not in {row['id'] for row in item['occurrences']}):
            raise PublicFactsError('Public facts activity occurrence counts do not match.')
    for issue in public_issues:
        if sum(row['issue_id'] == issue['id'] for item in items_by_id.values() for row in item['occurrences']) != issue['occurrence_count']:
            raise PublicFactsError('Public facts issue occurrence counts do not match.')

    body = {
        'schema_version': _SCHEMA_VERSION,
        'items': [items_by_id[key] for key in sorted(items_by_id)],
        'issues': sorted(public_issues, key=lambda row: row['id']),
    }
    body['data_version'] = hashlib.sha256(_json(body)).hexdigest()
    return body


def _read_json(path):
    _no_symlinks(path)
    if not path.is_file() or path.stat().st_size > _MAX_FACTS_BYTES:
        raise PublicFactsError('Public reader metadata is missing or exceeds the facts-size limit.')
    try:
        return json.loads(path.read_bytes())
    except (ValueError, UnicodeError):
        raise PublicFactsError('Public reader metadata is not valid JSON.') from None


def _check_manifest(source):
    manifest = _read_json(source / 'manifest.json')
    if (not isinstance(manifest, dict) or manifest.get('schema_version') != 2
            or manifest.get('generator') != GENERATOR or not isinstance(manifest.get('data_version'), str)
            or not _HASH.fullmatch(manifest['data_version']) or not isinstance(manifest.get('generated_at'), str)
            or not _valid_timestamp(manifest['generated_at']) or not isinstance(manifest.get('files'), dict)):
        raise PublicFactsError('A valid public reader-v2 manifest is required.')
    return manifest




def export_public_facts(source_dir, output_path):
    """Atomically update one compact public facts file from a published reader pack."""
    source = Path(source_dir).expanduser().absolute()
    output = Path(output_path).expanduser().absolute()
    _no_symlinks(source)
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        raise PublicFactsError('The public facts output directory could not be created.') from None
    _no_symlinks(output)
    if not source.is_dir() or not output.parent.is_dir():
        raise PublicFactsError('The public source and output directory must already exist.')
    manifest = _check_manifest(source)
    for key in ('catalog', 'issues'):
        relative = manifest['files'].get(key)
        if (not isinstance(relative, str) or not relative.startswith(f"versions/{manifest['data_version']}/")
                or not re.fullmatch(r'versions/[0-9a-f]{64}/[A-Za-z0-9._/-]+', relative)):
            raise PublicFactsError('The public manifest contains an invalid facts input path.')
        _no_symlinks(source / relative)
    catalog = _read_json(source / manifest['files']['catalog'])
    issues = _read_json(source / manifest['files']['issues'])
    facts = build_public_facts(catalog, issues)
    versioned = source / 'versions' / manifest['data_version'] / 'activity-facts.json'
    if versioned.exists() or versioned.is_symlink():
        if _read_json(versioned) != facts:
            raise PublicFactsError('The versioned public facts do not match the published catalog.')

    data = _json(facts)
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=f'.{output.name}.', suffix='.tmp', dir=output.parent)
        temporary = Path(name)
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if output.exists():
            if not stat.S_ISREG(output.stat().st_mode):
                raise PublicFactsError('The public facts destination must be a regular file.')
        os.replace(temporary, output)
        temporary = None
    except OSError:
        raise PublicFactsError('Public facts could not be written atomically.') from None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return {'data_version': facts['data_version'], 'activities': len(facts['items']),
            'occurrences': sum(item['occurrence_count'] for item in facts['items']),
            'bytes': len(data), 'output': str(output)}
