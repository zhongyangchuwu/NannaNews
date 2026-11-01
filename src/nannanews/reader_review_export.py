"""Publish private human revision heads into a new immutable public reader pack.

Preparation reads only the original public pack and the small private review store.
No source/model database is opened. Each detail shard is loaded separately; public
manifests switch only after both complete packs and share pages are prepared.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import urlsplit

from nannanews.public_metadata_export import build_public_facts
from nannanews.reader_reviews import ReviewError, ReviewStore, validate_payload
from nannanews.static_export import (
    GENERATOR, StaticExportError, _existing_manifest, _json, _manifest_files, _manifest_paths,
    _no_symlinks, _publish, _write_catalog_projections,
)

_HASH = re.compile(r'[0-9a-f]{64}\Z')
_ITEM = re.compile(r'item-[0-9a-f]{24}\Z')
_OCC = re.compile(r'occ-[0-9a-f]{24}\Z')
_ISSUE = re.compile(r'issue-[0-9a-f]{24}\Z')
_ASSET = re.compile(r'assets/[0-9a-f]{64}\.[a-z0-9]+\Z')
_COUNTS = ('items', 'occurrences', 'issues', 'documents', 'images', 'pdfs')
_MAX_JSON = 128 * 1024 * 1024
_NODE_TIMEOUT = 180


class ReviewPublishError(ReviewError):
    """Safe publish diagnostic, with truthful per-destination completion state."""

    status_code = 503

    def __init__(self, message, *, result=None):
        super().__init__(message)
        self.result = dict(result or {})
        self.result['partial'] = bool(self.result.get('source_published')
                                      or self.result.get('site_published'))


def _read_json(path, maximum=_MAX_JSON):
    _no_symlinks(path)
    if not path.is_file() or path.stat().st_size > maximum:
        raise ReviewPublishError('公开数据文件缺失、不是普通文件或超过发布大小限制。')
    try:
        return json.loads(path.read_bytes())
    except (ValueError, UnicodeError):
        raise ReviewPublishError('公开数据包含无效 JSON，尚未切换发布。') from None


def _relative(value, version, pattern):
    prefix = 'versions/' + version + '/'
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ReviewPublishError('公开数据资源没有绑定原始不可变版本。')
    relative = value[len(prefix):]
    if not re.fullmatch(pattern, relative):
        raise ReviewPublishError('公开数据资源路径无效。')
    return relative


def _manifest(root):
    result = _existing_manifest(root, True)
    if (not result or result['schema_version'] != 2 or result['generator'] != GENERATOR
            or any(type(result.get('counts', {}).get(key)) is not int
                   or result['counts'][key] < 0 for key in _COUNTS)
            or not isinstance(result.get('content_rules_version'), str)):
        raise ReviewPublishError('发布需要有效的第二版公开数据包。')
    return result


def _public_root(value, settings):
    if value is None:
        raise ReviewPublishError('请配置 NANNA_READER_DATA_DIR 和 NANNA_READER_SITE_DATA_DIR。')
    root = Path(value).expanduser().absolute()
    _no_symlinks(root)
    if not root.is_dir():
        raise ReviewPublishError('公开数据目录不存在；请先部署完整阅读器数据包。')
    root = root.resolve()
    private = Path(settings.data_dir).resolve()
    upload = Path(settings.upload_dir).resolve()
    database = Path(settings.database_path).resolve()
    if any(root.is_relative_to(path) or path.is_relative_to(root)
           for path in (private, upload, database)):
        raise ReviewPublishError('公开数据与私有运行目录必须互相独立。')
    return root


@contextmanager
def _publish_lock(settings):
    private = Path(settings.data_dir).absolute()
    _no_symlinks(private)
    if not private.is_dir():
        raise ReviewPublishError('私有控制台尚未初始化。')
    path = private / '.reader-review-publish.lock'
    _no_symlinks(path)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ReviewPublishError('人工核查发布锁不是普通文件。')
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ReviewPublishError('另一位管理员正在发布，请稍后明确重试。') from None
        yield
    finally:
        os.close(descriptor)


def _revisions(snapshot):
    result = {}
    if not _HASH.fullmatch(snapshot['base_version']):
        raise ReviewPublishError('原始数据版本无效。')
    for row in snapshot['revisions']:
        item_id = row['item_id']
        if (not isinstance(item_id, str) or not _ITEM.fullmatch(item_id) or item_id in result
                or not isinstance(row['revision'], str) or not _HASH.fullmatch(row['revision'])
                or row['base_version'] != snapshot['base_version']):
            raise ReviewPublishError('保存的人工核查版本与原始数据不匹配。')
        reviewed = datetime.fromisoformat(row['created_at'])
        if reviewed.tzinfo is None or reviewed.utcoffset() is None:
            raise ReviewPublishError('人工核查时间必须包含时区。')
        result[item_id] = {
            'revision': row['revision'], 'reviewed_at': row['created_at'],
            'payload': validate_payload(row['payload']),
        }
    return result
def _new_activities(snapshot, revisions, existing_items):
    rows = snapshot.get('new_activities', [])
    if not isinstance(rows, list):
        raise ReviewPublishError('新增活动登记快照格式无效。')
    result = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ReviewPublishError('新增活动登记快照格式无效。')
        item_id = row.get('item_id')
        occurrence_id = row.get('occurrence_id')
        source_id = row.get('source_id')
        try:
            registered_at = datetime.fromisoformat(row.get('created_at'))
        except (TypeError, ValueError):
            registered_at = None
        if registered_at is None or registered_at.tzinfo is None or registered_at.utcoffset() is None:
            raise ReviewPublishError('新增活动登记时间必须包含时区。')
        revision = revisions.get(item_id) if isinstance(item_id, str) else None
        if (not isinstance(item_id, str) or not _ITEM.fullmatch(item_id) or item_id in existing_items
                or item_id in result or row.get('activity_id') != item_id
                or not isinstance(occurrence_id, str) or not _OCC.fullmatch(occurrence_id)
                or not isinstance(source_id, str) or not re.fullmatch(r'source-[0-9a-f]{24}', source_id)
                or revision is None or row.get('revision') != revision['revision']
                or row.get('payload') != revision['payload'] or row.get('kind') != 'standalone'
                or row.get('issue_id') is not None or row.get('document_id') is not None
                or row.get('source_urls') != revision['payload']['activity']['source_urls']):
            raise ReviewPublishError('新增活动登记与保存的人工修订不匹配。')
        result[item_id] = row
    if set(revisions) - existing_items != set(result):
        raise ReviewPublishError('新增活动缺少稳定登记身份，或发布快照包含未知活动。')
    return result


def _standalone_occurrence(draft, revision):
    activity = revision['payload']['activity']
    verified = revision['payload']['status'] == 'verified'
    dates = [{'kind': value['kind'], 'normalized_date': value['date'],
              'resolution': 'explicit', 'literal_source': 'manual'} for value in activity['dates']] if verified else []
    deadlines = [value['normalized_date'] for value in dates if value['kind'] == 'deadline']
    events = [value['normalized_date'] for value in dates if value['kind'] == 'event']
    urls = activity['source_urls']
    return {
        'id': draft['occurrence_id'], 'item_id': draft['item_id'], 'activity_id': draft['activity_id'],
        'source_id': draft['source_id'], 'issue_id': None, 'document_id': None, 'kind': 'standalone',
        'title': activity['title'], 'category': activity['category'], 'preview': activity['summary'],
        'content_status': 'human_verified' if verified else 'needs_review', 'issue_date': None,
        'source_urls': urls, 'source_hosts': sorted({urlsplit(url).hostname.lower() for url in urls}),
        'colleges': [], 'dates': dates, 'expiry_date': max(deadlines or events, default=None),
        'image_count': 0,
        **({'activity_facts': {key: activity[key] for key in ('venue', 'audience', 'participation', 'time')}}
           if verified else {}),
        'manual_review': {'revision': revision['revision'], 'reviewed_at': revision['reviewed_at'],
                          'status': revision['payload']['status'], 'reason': revision['payload']['reason']},
    }


def _standalone_detail(row, revision):
    activity = revision['payload']['activity']
    dates = [{'kind': value['kind'], 'normalized_date': value['normalized_date'], 'raw_text': value['normalized_date'],
              'resolution': 'explicit', 'literal_source': 'manual', 'evidence': ''}
             for value in row['dates']]
    occurrence = dict(row, body_text='', dates=dates, college_mentions=[], images=[], primary_images=[],
                      locator={}, table_cells=[], date_review=None,
                      manual_review={**row['manual_review'], 'activity': activity})
    return {'id': row['item_id'], 'latest_occurrence_id': row['id'], 'occurrences': [occurrence]}




def _apply(row, revision, *, detail=False):
    payload = revision['payload']
    activity = payload['activity']
    verified = payload['status'] == 'verified'
    stamp = {key: revision[key] for key in ('revision', 'reviewed_at')}
    stamp.update(status=payload['status'], reason=payload['reason'])
    if detail:
        stamp['activity'] = activity
        if row.get('kind') != 'standalone' and 'original_dates' not in row:
            row['original_dates'] = row['dates']
    row['manual_review'] = stamp
    row['content_status'] = 'human_verified' if verified else 'needs_review'
    row['dates'] = [dict(kind=value['kind'], normalized_date=value['date'],
                         resolution='explicit', literal_source='manual',
                         **({'raw_text': value['date'], 'evidence': payload['reason']} if detail else {}))
                    for value in activity['dates']] if verified else []
    deadlines = [value['normalized_date'] for value in row['dates'] if value['kind'] == 'deadline']
    events = [value['normalized_date'] for value in row['dates'] if value['kind'] == 'event']
    row['expiry_date'] = max(deadlines or events, default=None)
    if verified:
        row['activity_facts'] = {key: activity[key] for key in ('venue', 'audience', 'participation', 'time')}
    else:
        row.pop('activity_facts', None)
    if not detail:
        row.update(title=activity['title'], preview=activity['summary'], category=activity['category'],
                   source_urls=activity['source_urls'],
                   source_hosts=sorted({urlsplit(url).hostname.lower() for url in activity['source_urls']}))


def _asset_path(value, old_version, new_version=None):
    relative = _relative(value, old_version, _ASSET.pattern)
    return 'versions/' + new_version + '/' + relative if new_version else relative


def _prepare(stage, source, snapshot, revisions):
    """Write native unprefixed payload, hash it, then prefix one shard at a time."""
    base = snapshot['base_manifest']
    old = snapshot['base_version']
    generated_at = datetime.now(timezone.utc).isoformat()
    try:
        _manifest_paths(base.get('files'), old)
    except (TypeError, ValueError):
        raise ReviewPublishError('原始公开数据清单无效。') from None
    if (base.get('data_version') != old or base.get('schema_version') != 2
            or base.get('generator') != GENERATOR
            or any(type(base.get('counts', {}).get(key)) is not int or base['counts'][key] < 0
                   for key in _COUNTS)):
        raise ReviewPublishError('原始公开数据清单无效。')
    catalog = _read_json(source / base['files']['catalog'])
    issues = _read_json(source / base['files']['issues'])
    search = _read_json(source / base['files']['search_documents'])
    counts = dict(base['counts'])
    if (not isinstance(catalog, dict) or not isinstance(catalog.get('items'), list)
            or not isinstance(catalog.get('occurrences'), list) or not isinstance(issues, list)
            or not isinstance(search, list) or len(catalog['items']) != counts['items']
            or len(catalog['occurrences']) != counts['occurrences']
            or len(issues) != counts['issues'] or len(search) != counts['occurrences']):
        raise ReviewPublishError('原始公开数据行数与清单不一致。')
    item_ids, shard_items = set(), {}
    for item in catalog['items']:
        item_id = item['id']
        if not _ITEM.fullmatch(item_id) or item_id in item_ids:
            raise ReviewPublishError('原始数据包含重复或无效活动编号。')
        item_ids.add(item_id)
        relative = _relative(item['detail_path'], old, r'items/[0-9a-f]{2}\.json')
        if relative != 'items/' + item_id[5:7] + '.json':
            raise ReviewPublishError('活动详情分片编号不匹配。')
        item['detail_path'] = relative
        shard_items.setdefault(relative, set()).add(item_id)
    new_activities = _new_activities(snapshot, revisions, item_ids)
    generated_details, generated_shards = {}, {}
    for item_id, draft in sorted(new_activities.items()):
        revision = revisions[item_id]
        occurrence = _standalone_occurrence(draft, revision)
        relative = 'items/' + item_id[5:7] + '.json'
        catalog['items'].append({'id': item_id, 'latest_occurrence_id': occurrence['id'],
                                 'occurrence_count': 1, 'aliases': [], 'detail_path': relative})
        catalog['occurrences'].append(occurrence)
        generated_details[item_id] = _standalone_detail(occurrence, revision)
        generated_shards.setdefault(relative, set()).add(item_id)
        activity = revision['payload']['activity']
        search.append({'id': occurrence['id'], 'title': activity['title'], 'text': '\n'.join(value for value in (
            *(activity[key] for key in ('title', 'summary', 'content', 'category', 'time',
                                       'venue', 'organizer', 'audience', 'participation')),
            *(value['date'] for value in activity['dates']), *activity['source_urls'],
        ) if value is not None)})
        item_ids.add(item_id)
    counts['items'] += len(new_activities)
    counts['occurrences'] += len(new_activities)
    issue_ids, document_ids, assets = set(), set(), set()
    for issue in issues:
        if not _ISSUE.fullmatch(issue['id']) or issue['id'] in issue_ids:
            raise ReviewPublishError('公开刊期编号无效或重复。')
        issue_ids.add(issue['id'])
        for document in issue['documents']:
            if not _HASH.fullmatch(document['id']) or document['id'] in document_ids:
                raise ReviewPublishError('公开来源文档编号无效或重复。')
            document_ids.add(document['id'])
            if document['pdf_path']:
                document['pdf_path'] = _asset_path(document['pdf_path'], old)
                assets.add(document['pdf_path'])
    if len(document_ids) != counts['documents']:
        raise ReviewPublishError('公开来源文档数量不匹配。')
    occurrences, affected_issues, affected_count = {}, set(), 0
    for occurrence in catalog['occurrences']:
        identity = occurrence['id']
        standalone = occurrence.get('kind') == 'standalone'
        references_invalid = ((occurrence.get('issue_id') is not None or occurrence.get('document_id') is not None
                               or occurrence.get('activity_id') != occurrence['item_id']
                               or not isinstance(occurrence.get('source_id'), str)
                               or not re.fullmatch(r'source-[0-9a-f]{24}', occurrence['source_id']))
                              if standalone else (occurrence.get('issue_id') not in issue_ids
                                                  or occurrence.get('document_id') not in document_ids))
        if (not isinstance(identity, str) or not _OCC.fullmatch(identity) or identity in occurrences
                or occurrence.get('item_id') not in item_ids or references_invalid):
            raise ReviewPublishError('公开活动出现记录引用无效或重复。')
        occurrences[identity] = occurrence['item_id']
        if revision := revisions.get(occurrence['item_id']):
            _apply(occurrence, revision)
            if occurrence.get('issue_id') is not None:
                affected_issues.add(occurrence['issue_id'])
            affected_count += 1
    search_ids = set()
    for document in search:
        identity = document['id']
        if (identity not in occurrences or identity in search_ids
                or not isinstance(document['title'], str) or not isinstance(document['text'], str)):
            raise ReviewPublishError('公开搜索记录引用无效或重复。')
        search_ids.add(identity)
        if revision := revisions.get(occurrences[identity]):
            activity = revision['payload']['activity']
            document['title'] = activity['title']
            document['text'] = '\n'.join(value for value in (
                *(activity[key] for key in ('title', 'summary', 'content', 'category', 'time',
                                           'venue', 'organizer', 'audience', 'participation')),
                *(value['date'] for value in activity['dates']), *activity['source_urls'],
            ) if value is not None)
    payload = stage / 'payload'
    payload.mkdir()
    seen_occurrences = set()
    for relative in sorted(set(shard_items) | set(generated_shards)):
        expected = shard_items.get(relative, set())
        shard = _read_json(source / 'versions' / old / relative) if expected else {}
        if not isinstance(shard, dict) or set(shard) != expected:
            raise ReviewPublishError('公开详情分片活动集合不匹配。')
        for item_id in sorted(generated_shards.get(relative, ())):
            if item_id in shard:
                raise ReviewPublishError('新增活动与现有详情分片编号冲突。')
            shard[item_id] = generated_details[item_id]
        for item_id, detail in shard.items():
            if detail['id'] != item_id or not isinstance(detail['occurrences'], list):
                raise ReviewPublishError('公开活动详情身份不匹配。')
            for occurrence in detail['occurrences']:
                identity = occurrence['id']
                if (identity in seen_occurrences or occurrences.get(identity) != item_id
                        or occurrence['item_id'] != item_id):
                    raise ReviewPublishError('公开详情出现记录不匹配。')
                seen_occurrences.add(identity)
                for key in ('images', 'primary_images'):
                    for image in occurrence[key]:
                        image['path'] = _asset_path(image['path'], old)
                        assets.add(image['path'])
                if item_id not in new_activities and (revision := revisions.get(item_id)):
                    _apply(occurrence, revision, detail=True)
        target = payload / relative
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(_json(shard))
        # Loop variables otherwise keep the final nested record alive while
        # the next shard is decoded.
        del shard
        detail = occurrence = image = None
    if seen_occurrences != set(occurrences) or len(assets) != counts['images'] + counts['pdfs']:
        raise ReviewPublishError('公开详情或不可变资源数量不匹配。')
    facts = build_public_facts(catalog, issues)
    files = {'catalog.json': catalog, 'issues.json': issues, 'search-documents.json': search,
             'activity-facts.json': facts}
    for name, value in files.items():
        (payload / name).write_bytes(_json(value))
    del catalog, issues, search, facts, files, value
    names = sorted(['catalog.json', 'issues.json', 'search-documents.json', 'activity-facts.json',
                    *shard_items, *generated_shards])
    digest = hashlib.sha256()
    digest.update(_json({'schema_version': 2, 'generator': GENERATOR, 'counts': counts,
                         'content_rules_version': base['content_rules_version']}))
    for name in names:
        digest.update(_json(name))
        digest.update((payload / name).read_bytes())
    for name in sorted(assets):
        original = source / 'versions' / old / name
        _no_symlinks(original)
        if not original.is_file():
            raise ReviewPublishError('原始不可变图片或 PDF 缺失。')
        digest.update(_json(name))
        with original.open('rb') as stream:
            digest.update(hashlib.file_digest(stream, 'sha256').digest())
        target = payload / name
        target.parent.mkdir(exist_ok=True)
        os.link(original, target)
    version = digest.hexdigest()
    prefix = 'versions/' + version + '/'
    for name in names:
        if name == 'search-documents.json':
            continue
        value = _read_json(payload / name)
        if name == 'catalog.json':
            for item in value['items']:
                item['detail_path'] = prefix + item['detail_path']
        elif name == 'issues.json':
            for issue in value:
                for document in issue['documents']:
                    if document['pdf_path']:
                        document['pdf_path'] = prefix + document['pdf_path']
        elif name != 'activity-facts.json':
            for detail in value.values():
                for occurrence in detail['occurrences']:
                    for key in ('images', 'primary_images'):
                        for image in occurrence[key]:
                            image['path'] = prefix + image['path']
        (payload / name).write_bytes(_json(value))
        del value
        item = issue = document = detail = occurrence = image = None
    projections = _write_catalog_projections(payload, _read_json(payload / 'catalog.json'),
                                             _read_json(payload / 'issues.json'), version)
    (stage / 'versions').mkdir()
    os.rename(payload, stage / 'versions' / version)
    manifest = dict(base, data_version=version, files={**_manifest_files(version), **projections}, counts=counts,
                    generated_at=generated_at, review_base_version=old)
    (stage / 'manifest.json').write_bytes(_json(manifest))
    return version, affected_count, sorted(affected_issues)


def _run_builder(node, script, arguments, label):
    try:
        subprocess.run([node, str(script), *map(str, arguments)], shell=False,
                       check=True, timeout=_NODE_TIMEOUT, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, env={**os.environ, 'NODE_OPTIONS': ''})
    except subprocess.TimeoutExpired:
        raise ReviewPublishError(f'{label}超时；未自动重试，请检查后明确重试。') from None
    except (subprocess.CalledProcessError, OSError):
        raise ReviewPublishError(f'{label}失败；请检查本机 Node、依赖和构建脚本配置后重试。') from None


def _copy_stage(source, target):
    shutil.copytree(source / 'versions', target / 'versions', copy_function=os.link)
    shutil.copyfile(source / 'manifest.json', target / 'manifest.json')


def _validate_share_destination(prepared, output):
    for source in (prepared / 'share').rglob('index.html'):
        relative = source.relative_to(prepared)
        target = output / relative
        _no_symlinks(target)
        if target.exists() and not target.is_file():
            raise ReviewPublishError('分享页面目标不是普通文件。')
        for directory in target.parents:
            if directory == output:
                break
            if directory.exists() and not directory.is_dir():
                raise ReviewPublishError('分享页面目标不是普通目录。')


def _install_shares(prepared, output):
    # Each page changes atomically. A cross-page failure is explicitly partial;
    # the same saved snapshot can be published again without editing history.
    for source in sorted((prepared / 'share').rglob('index.html')):
        relative = source.relative_to(prepared)
        directory = output
        for part in relative.parts[:-1]:
            directory = directory / part
            _no_symlinks(directory)
            directory.mkdir(exist_ok=True)
            if not directory.is_dir():
                raise ReviewPublishError('分享页面目标不是普通目录。')
        target = output / relative
        _no_symlinks(target)
        if target.exists() and not target.is_file():
            raise ReviewPublishError('分享页面目标不是普通文件。')
        descriptor, temporary = tempfile.mkstemp(prefix='.share-', dir=directory)
        os.close(descriptor)
        try:
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)


def publish_reviews(*, settings=None, actor):
    """Explicitly publish one exact snapshot; never retry or rebuild frontend JS.

    ReviewPublishError.result reports switched manifests even if a later step
    fails. record_publication is only called once all destinations and shares
    succeeded, so concurrent later saves remain unpublished.
    """
    if settings is None:
        from nannanews import runtime
        settings = runtime.settings
    result = {'version': None, 'source_published': False, 'site_published': False,
              'share_updated': False, 'recorded': False, 'partial': False}
    stages = []
    try:
        if (not isinstance(actor, str) or not actor.strip() or len(actor) > 250
                or any(ord(character) < 32 for character in actor)):
            raise ReviewPublishError('发布人身份无效，未发布。')
        source = _public_root(settings.reader_data_dir, settings)
        site = _public_root(settings.reader_site_data_dir, settings)
        if source != site and (source.is_relative_to(site) or site.is_relative_to(source)):
            raise ReviewPublishError('源数据和部署数据不能互相嵌套。')
        output = site.parent
        _no_symlinks(output)
        builder = settings.reader_search_builder
        if builder is None:
            raise ReviewPublishError('请配置 NANNA_READER_SEARCH_BUILDER 为现有 build-search.mjs。')
        builder = Path(builder).expanduser().absolute()
        share_builder = builder.with_name('build-share.mjs')
        for script in (builder, share_builder):
            _no_symlinks(script)
            if not script.is_file():
                raise ReviewPublishError('本机搜索或分享构建脚本不可用。')
        node = shutil.which('node')
        if node is None:
            raise ReviewPublishError('本机 Node 不可用，无法构建真实搜索索引。')
        with _publish_lock(settings):
            store = ReviewStore(settings)
            snapshot = store.publication_snapshot()
            revisions = _revisions(snapshot)
            if not revisions:
                raise ReviewPublishError('没有已保存的人工核查修改可发布。')
            source_previous = _manifest(source)
            site_previous = _manifest(site)
            if source_previous != snapshot['active_manifest']:
                raise ReviewPublishError('公开源数据已变化，请重新查看后发布。')
            stage = Path(tempfile.mkdtemp(prefix='.reader-review-', dir=source.parent))
            stages.append(stage)
            version, affected_count, issue_ids = _prepare(stage, source, snapshot, revisions)
            result.update(version=version, revision_count=len(revisions), occurrence_count=affected_count)
            _run_builder(node, builder, [stage], '搜索索引构建')
            staged_manifest = _read_json(stage / 'manifest.json')
            expected_count = staged_manifest.get('counts', {}).get('occurrences')
            index = _read_json(stage / _manifest_files(version)['search_index'])
            if (not isinstance(index, dict) or index.get('documentCount') != expected_count
                    or index.get('serializationVersion') != 2 or not isinstance(index.get('index'), list)):
                raise ReviewPublishError('搜索构建器未生成完整 MiniSearch 索引。')
            del index, staged_manifest
            shares = Path(tempfile.mkdtemp(prefix='.reader-share-', dir=output))
            stages.append(shares)
            subset = shares / 'subset.json'
            subset.write_bytes(_json({'item_ids': sorted(revisions), 'issue_ids': issue_ids}))
            _run_builder(node, share_builder, ['--data-root', stage, '--output-root', shares,
                                              '--subset-file', subset], '分享页面构建')
            expected_pages = affected_count + len(issue_ids)
            if len(list((shares / 'share').rglob('index.html'))) != expected_pages:
                raise ReviewPublishError('分享构建器未生成全部受影响页面。')
            if source == site:
                site_stage = stage
            else:
                site_stage = Path(tempfile.mkdtemp(prefix='.reader-review-', dir=site.parent))
                stages.append(site_stage)
                _copy_stage(stage, site_stage)
            _validate_share_destination(shares, output)
            # Last guard before the first visible mutation. Native _publish also
            # compares each destination's manifest immediately before its switch.
            if _manifest(source) != snapshot['active_manifest'] or _manifest(site) != site_previous:
                raise ReviewPublishError('准备期间公开数据已变化，未切换发布。')
            store.record_preparation(actor=actor, snapshot=snapshot, result=result)
            _publish(stage, source, version, source_previous)
            result['source_published'] = True
            if source != site:
                _publish(site_stage, site, version, site_previous)
            result['site_published'] = True
            _install_shares(shares, output)
            result['share_updated'] = True
            store.record_publication(actor=actor, snapshot=snapshot, result={**result, 'recorded': True})
            result['recorded'] = True
            return result
    except ReviewPublishError as error:
        raise ReviewPublishError(str(error), result={**result, **error.result}) from None
    except ReviewError as error:
        if result['source_published']:
            raise ReviewPublishError('公开数据已切换，但私有发布记录未完成；请明确重试。', result=result) from None
        raise error
    except (OSError, StaticExportError, ValueError, KeyError, TypeError, AttributeError):
        message = ('公开数据已部分切换，部署或记录未完成；旧版本已保留，请明确重试。'
                   if result['source_published'] else '发布准备失败；请检查公开数据、权限和磁盘，现有清单未切换。')
        raise ReviewPublishError(message, result=result) from None
    finally:
        for stage in reversed(stages):
            shutil.rmtree(stage, ignore_errors=True)
