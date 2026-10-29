"""Private, canonical-wide human review of the immutable public reader pack."""

from io import BytesIO
import secrets
from difflib import unified_diff
from itertools import zip_longest
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlsplit
import re
import sqlite3

from flask import abort, flash, jsonify, redirect, render_template, request, send_file, session, url_for

from nannanews import runtime
from nannanews.reader_reviews import ReviewError, ReviewStore, ReviewValidationError, validate_public_url
from nannanews.reader_review_export import ReviewPublishError, publish_reviews
from .security import admin_required, editor_required, login_required


STATUS_OPTIONS = (
    ('problems', '所有问题活动'), ('needs_review', '待人工核对'),
    ('conflict', '来源或日期冲突'), ('unconfirmed', '尚未确认'),
    ('unresolved_dates', '日期待核对'),
    ('unreviewed', '尚未核查'), ('legacy', '历史规则记录'),
    ('edited', '已人工编辑'), ('unpublished', '已保存未发布'),
    ('stale', '来源已更新，须重新核对'), ('removed', '来源已移除 / 合并，待确认'),
    ('retired', '已确认停用的历史修订'), ('all', '全部活动'),
)
STATUS_LABELS = dict(STATUS_OPTIONS, verified='AI 已对照原文', human_verified='人工核实',
                     source_corrected='来源链接已纠正')
DATE_KINDS = (('event', '活动日期'), ('deadline', '截止日期'),
              ('registration', '报名日期'), ('application', '申请日期'))
TEXT_FIELDS = (
    ('title', '活动标题', 250), ('summary', '摘要', 1500),
    ('content', '完整内容', 60000), ('category', '类别', 200),
    ('time', '时间说明', 1000), ('venue', '地点', 500),
    ('organizer', '主办方', 500), ('audience', '参与对象', 1500),
    ('participation', '参与方式', 3000),
)
FIELD_LABELS = dict((name, label) for name, label, _ in TEXT_FIELDS)
FIELD_LABELS.update(status='核查结论', reason='公开说明', source_urls='来源链接', dates='日期')


def _safe_http_url(value):
    try:
        return validate_public_url(value)
    except ReviewValidationError:
        return None


def _submitted_payload():
    activity = {name: request.form.get(name, '') for name, _, _ in TEXT_FIELDS}
    activity['source_urls'] = [line.strip() for line in request.form.get('source_urls', '').splitlines() if line.strip()]
    activity['dates'] = [
        {'kind': kind, 'date': day}
        for kind, day in zip_longest(request.form.getlist('date_kind'), request.form.getlist('date_value'), fillvalue='')
    ]
    for name, _, _ in TEXT_FIELDS:
        if name not in ('title', 'summary', 'content') and not activity[name].strip():
            activity[name] = None
    return {'status': request.form.get('status', ''), 'reason': request.form.get('reason', ''), 'activity': activity}


def _field_text(value):
    if value is None:
        return ''
    if isinstance(value, list):
        return '\n'.join(f"{dict(DATE_KINDS).get(row.get('kind'), row.get('kind', ''))}: {row.get('date', '')}"
                         if isinstance(row, dict) else str(row) for row in value)
    return str(value)


def _payload_diff(before, after):
    changes = []
    before = before or {'activity': {}}
    for key in ('status', 'reason', *(name for name, _, _ in TEXT_FIELDS), 'source_urls', 'dates'):
        left = before.get(key) if key in ('status', 'reason') else before.get('activity', {}).get(key)
        right = after.get(key) if key in ('status', 'reason') else after.get('activity', {}).get(key)
        left, right = _field_text(left), _field_text(right)
        if left != right:
            diff = '\n'.join(unified_diff(left.splitlines(), right.splitlines(),
                                          fromfile='此前', tofile='此版本', lineterm=''))
            changes.append({'label': FIELD_LABELS[key], 'diff': diff})
    return changes


def _reader_base_url():
    base = getattr(getattr(runtime, 'settings', None), 'reader_url', '')
    if not isinstance(base, str) or not base or any(
            ord(char) <= 0x20 or ord(char) == 0x7f or char == '\\' for char in base):
        return None
    try:
        parsed = urlsplit(base)
        _ = parsed.port
    except ValueError:
        return None
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment or '?' in base or '#' in base):
        return None
    return base.rstrip('/')


def _reader_href(item_id, occurrence_id=None):
    base = _reader_base_url()
    if base is None:
        return None
    href = f"{base}/#/items/{quote(item_id, safe='')}"
    if occurrence_id:
        href += f"?occ={quote(occurrence_id, safe='')}"
    return href


def _reader_issue_href(issue_id):
    if not isinstance(issue_id, str) or re.fullmatch(r'issue-[a-f0-9]{24}', issue_id) is None:
        return None
    base = _reader_base_url()
    if base is None:
        return None
    return f"{base}/#/issues/{quote(issue_id, safe='')}"


def _unavailable(error):
    if error.status_code == 404:
        abort(404)
    return render_template('reader_review_unavailable.html', error=str(error)), error.status_code

def _queue_context():
    status = request.args.get('queue_status', 'problems')
    if status not in dict(STATUS_OPTIONS):
        status = 'problems'
    page = request.args.get('queue_page', 1, type=int)
    return {'queue_q': request.args.get('queue_q', ''), 'queue_status': status,
            'queue_page': max(1, page or 1)}


def _queue_template_context():
    return {'review_queue_context': _queue_context()} if request.path.startswith('/console/') else {}



@login_required
@editor_required
def activities():
    status = request.args.get('status', 'problems')
    if status not in dict(STATUS_OPTIONS):
        status = 'problems'
    page = request.args.get('page', 1, type=int)
    try:
        result = ReviewStore().list_items(q=request.args.get('q', ''), status=status, page=max(1, page or 1))
    except ReviewError as error:
        return _unavailable(error)
    return render_template('reader_review_queue.html', result=result, status_options=STATUS_OPTIONS,
                           status_labels=STATUS_LABELS,
                           review_queue_context={'queue_q': result['q'], 'queue_status': result['status'],
                                                 'queue_page': result['page']})


def _item_page(item, payload, expected_head, base_version, *, error=None, code=200, conflict=False,
               retirement_reason='', retirement_acknowledged=False):
    history = item['history']
    revisions = []
    for index, revision in enumerate(history):
        previous = history[index + 1]['payload'] if index + 1 < len(history) else None
        revisions.append({'record': revision, 'changes': _payload_diff(previous, revision['payload'])})
    latest_payload = item['head']['payload'] if item['head'] else item['initial_payload']
    review_available, review_unavailable = False, None
    if item.get('registration') and not item.get('removed'):
        try:
            ReviewStore().evidence().root()
            review_available = True
        except ReviewError as exc:
            review_unavailable = str(exc)
    return render_template(
        'reader_review_item.html', item=item, payload=payload, expected_head=expected_head,
        base_version=base_version, error=error, conflict=conflict, revisions=revisions,
        comparison=_payload_diff(latest_payload, payload) if conflict else [],
        text_fields=TEXT_FIELDS, date_kinds=DATE_KINDS, status_labels=STATUS_LABELS,
        safe_http_url=_safe_http_url, reader_href=_reader_href(item['item_id']),
        reader_occurrence_href=_reader_href, reader_issue_href=_reader_issue_href,
        retirement_reason=retirement_reason, retirement_acknowledged=retirement_acknowledged,
        review_available=review_available, review_unavailable=review_unavailable,
    ), code


@admin_required
def _retire_removed(store, item):
    expected_head = request.form.get('expected_head', '')
    base_version = request.form.get('base_version', '')
    reason = request.form.get('retirement_reason', '')
    acknowledged = request.form.get('acknowledge_retirement') == 'yes'
    try:
        if not acknowledged:
            raise ReviewValidationError('请明确勾选确认停用此已移除活动的当前修订；不会自动停用或发布。')
        store.retire_removed(item['item_id'], actor=session['username'],
                             expected_head=expected_head or None, base_version=base_version, reason=reason)
    except ReviewError as error:
        if error.status_code == 409:
            try:
                item = store.get_item(item['item_id'])
            except ReviewError:
                pass  # Preserve the submitted retirement request if the pack became unavailable.
        payload = item['head']['payload'] if item['head'] else item['initial_payload']
        return _item_page(item, payload, expected_head, base_version, error=str(error),
                          code=error.status_code, conflict=error.status_code == 409,
                          retirement_reason=reason, retirement_acknowledged=acknowledged)
    flash('已私有审计确认停用此已移除活动的当前修订；历史与原始证据仍保留，未保存活动内容，也未发布。')
    return redirect(url_for('reader_review_activity', item_id=item['item_id'], **_queue_context()))


@login_required
@editor_required
def activity(item_id):
    store = ReviewStore()
    try:
        item = store.get_item(item_id)
    except ReviewError as error:
        return _unavailable(error)
    if request.method == 'GET':
        payload = item['head']['payload'] if item['head'] else item['initial_payload']
        bound_version = (item['head']['base_version']
                         if item['stale'] and item['head'] and not item.get('removed') else item['base_version'])
        return _item_page(item, payload, item['head_revision'] or '', bound_version,
                          conflict=item['stale'] and not item.get('removed'))

    if request.form.get('action') == 'retire_removed':
        return _retire_removed(store, item)

    payload = _submitted_payload()
    expected_head = request.form.get('expected_head', '')
    base_version = request.form.get('base_version', '')
    if item.get('removed'):
        return _item_page(item, payload, expected_head, base_version,
                          error='此活动已从当前来源移除或合并，不能保存普通修订。历史仍保留；仅管理员可另行确认停用当前历史修订。',
                          code=409)
    if request.form.get('action') == 'rebase':
        if (request.form.get('rebase_head') != (item['head_revision'] or '')
                or request.form.get('rebase_base') != item['base_version']):
            return _item_page(item, payload, expected_head, base_version,
                              error='核对期间又出现了新版本；请先对照更新后的差异，再确认继续。',
                              code=409, conflict=True)
        if request.form.get('acknowledge_latest') != 'yes':
            return _item_page(item, payload, expected_head, base_version,
                              error='请先对照最新原刊与已保存版本，并勾选确认。你的输入尚未保存。',
                              code=409, conflict=True)
        return _item_page(item, payload, item['head_revision'] or '', item['base_version'],
                          error='已明确选择以最新版本为基准；你的输入仍未保存。请复核下列字段，再点击“保存修改”。')
    try:
        store.save(item_id, payload, actor=session['username'],
                   expected_head=expected_head or None, base_version=base_version)
    except ReviewError as error:
        if error.status_code == 409:
            try:
                item = store.get_item(item_id)
            except ReviewError:
                pass  # Keep the submitted form even if the public pack became unavailable.
        return _item_page(item, payload, expected_head, base_version, error=str(error),
                          code=error.status_code, conflict=error.status_code == 409 or item['stale'])
    flash('修改已保存到私有核查记录，尚未发布；公开阅读页没有因此更新。')
    return redirect(url_for('reader_review_activity', item_id=item_id, **_queue_context()))


@login_required
@admin_required
def publish():
    if request.form.get('confirm_publish') != 'yes':
        return render_template('reader_review_publish.html', error='请确认发布范围后提交。', result=None), 400
    try:
        result = publish_reviews(actor=session['username'])
    except ReviewPublishError as error:
        return render_template('reader_review_publish.html', error=str(error), result=error.result), 503
    except ReviewError as error:
        return render_template('reader_review_publish.html', error=str(error), result=None), error.status_code
    flash(f"发布完成：{result['revision_count']} 个活动的已保存版本已同步到公开阅读数据；版本 {result['version'][:12]}。")
    return redirect(url_for('reader_review_activities', status='edited'))


@login_required
@editor_required
def image(item_id, image_id):
    """Serve only an image already admitted to this item's immutable public pack."""
    try:
        item = ReviewStore().get_item(item_id)
    except ReviewError as error:
        return _unavailable(error)
    images = (entry for occurrence in item['occurrences']
              for collection in ('images', 'primary_images') for entry in occurrence.get(collection, []))
    admitted = next((entry for entry in images if isinstance(entry, dict) and entry.get('id') == image_id), None)
    if not admitted or admitted.get('mime') not in ('image/jpeg', 'image/png', 'image/webp', 'image/gif'):
        abort(404)
    path = admitted.get('path')
    if not isinstance(path, str) or not path or '\\' in path or any(ord(char) < 32 for char in path):
        abort(404)
    relative = PurePosixPath(path)
    if relative.is_absolute() or '..' in relative.parts:
        abort(404)
    root = runtime.settings.reader_data_dir
    candidate = root.joinpath(*relative.parts)
    try:
        if any(part.is_symlink() for part in (candidate, *candidate.parents)):
            abort(404)
        candidate.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        abort(404)
    if not candidate.is_file():
        abort(404)
    response = send_file(candidate, mimetype=admitted['mime'], conditional=True)
    response.headers['Cache-Control'] = 'private, max-age=300'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@login_required
@editor_required
def register_activity():
    error = None
    registration_key = request.form.get('registration_key', '') or secrets.token_urlsafe(24)
    if request.method == 'POST':
        try:
            result = ReviewStore().register_activity(registration_key, _submitted_payload(), actor=session['username'])
        except ReviewError as exc:
            error = exc
        else:
            flash('活动与来源已私有登记；请核对并保存修订，之后仅管理员可显式发布。')
            return redirect(url_for('reader_review_activity', item_id=result['item_id'], **_queue_context()))
    return render_template('reader_review_new.html', registration_key=registration_key,
                           fields=TEXT_FIELDS, submitted=request.form, error=str(error) if error else None), (
                               error.status_code if error else 200)


@login_required
@editor_required
def reviews(item_id):
    store = ReviewStore()
    try:
        if request.method == 'POST':
            store.associate_review(item_id, request.form.get('review_id', ''), actor=session['username'])
            flash('已记录私有核查证据关联；没有修改原始记录或公开发布。')
            return redirect(url_for('reader_review_reviews', item_id=item_id, **_queue_context()))
        result = store.list_reviews(item_id, page=request.args.get('page', 1, type=int))
        item = result['activity']
    except ReviewError as error:
        return _unavailable(error)
    return render_template('reader_review_reviews.html', item=item, result=result)


def _selected_evidence(item_id, review_id):
    store = ReviewStore()
    evidence = store.evidence()
    # Resolve the record first; an association from a removed or different
    # dataset must never authorize a newly unique NULL-owned review ID.
    dataset = evidence.resolve_review(review_id)['dataset_id']
    associated = store.review_associated(item_id, review_id, dataset=dataset)
    return evidence, associated, dataset


@login_required
@editor_required
def review(item_id, review_id):
    try:
        evidence, associated, dataset = _selected_evidence(item_id, review_id)
        result = evidence.review(item_id, review_id, associated=associated, dataset=dataset)
    except ReviewError as error:
        return _unavailable(error)
    return render_template('reader_review_trace.html', item_id=item_id, trace=result)


def _private_bytes(raw, mime, filename=None):
    response = send_file(BytesIO(raw), mimetype=mime, as_attachment=filename is not None,
                         download_name=filename, max_age=0)
    response.headers['Cache-Control'] = 'private, no-store'
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Content-Security-Policy'] = "default-src 'none'; sandbox"
    return response

@login_required
@editor_required
def documents():
    try:
        result = ReviewStore().evidence().documents(
            query=request.args.get('q', ''),
            page=request.args.get('page', 1, type=int))
    except ReviewError as error:
        return _unavailable(error)
    return render_template('reader_review_documents.html', result=result)


@login_required
@editor_required
def document_pdf(document_id):
    try:
        raw = ReviewStore().evidence().document_pdf(document_id)
    except ReviewError as error:
        return _unavailable(error)
    return _private_bytes(raw, 'application/pdf', document_id + '.pdf')



@login_required
@editor_required
def review_artifact(item_id, review_id, role):
    try:
        evidence, associated, dataset = _selected_evidence(item_id, review_id)
        raw = evidence.artifact(item_id, review_id, role, associated=associated, dataset=dataset)
    except ReviewError as error:
        return _unavailable(error)
    return _private_bytes(raw, 'application/octet-stream', role + ('.bin' if role == 'response' else '.json'))


@login_required
@editor_required
def review_image(item_id, review_id, image_index):
    try:
        evidence, associated, dataset = _selected_evidence(item_id, review_id)
        raw, mime = evidence.prepared_image(item_id, review_id, image_index, associated=associated, dataset=dataset)
    except ReviewError as error:
        return _unavailable(error)
    return _private_bytes(raw, mime)


@login_required
@editor_required
def review_source(item_id, review_id, snapshot_id):
    try:
        evidence, associated, dataset = _selected_evidence(item_id, review_id)
        result = evidence.source(item_id, review_id, snapshot_id, associated=associated, dataset=dataset)
    except ReviewError as error:
        return _unavailable(error)
    return render_template('reader_review_source.html', item_id=item_id, review_id=review_id, source=result)


def _api_body():
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise ReviewValidationError('请求必须是 JSON 对象。')
    return body

def _api_result(value):
    if isinstance(value, dict):
        return {key: _api_result(item) for key, item in value.items() if key != 'dataset_id'}
    if isinstance(value, list):
        return [_api_result(item) for item in value]
    return value



@login_required
@editor_required
def api_activities():
    store = ReviewStore()
    try:
        if request.method == 'POST':
            body = _api_body()
            return jsonify(store.register_activity(body.get('registration_key'), body.get('payload'),
                                                   actor=session['username'])), 201
        return jsonify(store.list_items(q=request.args.get('q', ''), status=request.args.get('status', 'all'),
                                        page=request.args.get('page', 1, type=int)))
    except ReviewError as error:
        return jsonify(error=str(error)), error.status_code


@login_required
@editor_required
def api_activity(item_id):
    store = ReviewStore()
    try:
        if request.method == 'PUT':
            body = _api_body()
            return jsonify(store.save(item_id, body.get('payload'), actor=session['username'],
                                       expected_head=body.get('expected_head'), base_version=body.get('base_version')))
        return jsonify(_api_result(store.get_item(item_id)))
    except ReviewError as error:
        return jsonify(error=str(error)), error.status_code


@login_required
@editor_required
def api_reviews(item_id):
    store = ReviewStore()
    try:
        if request.method == 'POST':
            body = _api_body()
            return jsonify(_api_result(store.associate_review(item_id, body.get('review_id'), actor=session['username']))), 201
        return jsonify(_api_result(store.list_reviews(item_id, page=request.args.get('page', 1, type=int))))
    except ReviewError as error:
        return jsonify(error=str(error)), error.status_code


@login_required
@editor_required
def api_review(item_id, review_id):
    try:
        evidence, associated, dataset = _selected_evidence(item_id, review_id)
        return jsonify(_api_result(evidence.review(item_id, review_id, associated=associated, dataset=dataset)))
    except ReviewError as error:
        return jsonify(error=str(error)), error.status_code


@login_required
@editor_required
def api_source(item_id, review_id, snapshot_id):
    try:
        evidence, associated, dataset = _selected_evidence(item_id, review_id)
        return jsonify(_api_result(evidence.source(item_id, review_id, snapshot_id, associated=associated, dataset=dataset)))
    except ReviewError as error:
        return jsonify(error=str(error)), error.status_code


@login_required
@admin_required
def api_publish():
    try:
        body = _api_body()
    except ReviewError as error:
        return jsonify(error=str(error)), error.status_code
    if body.get('confirm_publish') is not True:
        return jsonify(error='必须显式确认发布全部已保存修订。'), 400
    try:
        return jsonify(publish_reviews(actor=session['username']))
    except ReviewPublishError as error:
        return jsonify(error=str(error), result=error.result), 503
    except ReviewError as error:
        return jsonify(error=str(error)), error.status_code


def _run_registered_review(item_id, body):
    if (not isinstance(body, dict) or set(body) != {'confirm_fetch', 'confirm_paid'}
            or body['confirm_fetch'] is not True or body['confirm_paid'] is not True):
        raise ReviewValidationError('必须分别明确授权公开来源读取和一次可能收费的模型核查。')
    store = ReviewStore()
    item = store.get_item(item_id)
    if not item.get('registration') or item.get('removed'):
        raise ReviewValidationError('仅可对具有明确稳定身份的私有登记活动执行核查。')
    root = store.evidence().root()
    from nannanews.reader_activity_review import review_registered_activity
    return review_registered_activity(store=store, item_id=item_id, actor=session['username'],
        allow_fetch=True, allow_paid=True, foundation_root=root)


def _review_result_status(result):
    if result['state'] == 'reviewed':
        return 200
    return 409 if result['state'] == 'admission_refused' else 422


@login_required
@editor_required
def run_activity_review(item_id):
    result, error, code = None, None, 200
    try:
        if set(request.form) - {'csrf_token', 'confirm_fetch', 'confirm_paid'}:
            raise ReviewValidationError('核查请求含有未知字段；不能提交资料库路径或模型配置。')
        body = {'confirm_fetch': request.form.get('confirm_fetch') == 'yes',
                'confirm_paid': request.form.get('confirm_paid') == 'yes'}
        result = _run_registered_review(item_id, body)
        code = _review_result_status(result)
    except ReviewError as exc:
        error, code = str(exc), exc.status_code
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        error, code = f'核查流程未能完整完成：{exc}。可能已有私有证据或付费尝试记录；请先检查，不能自动重试。', 503
    return render_template('reader_review_run.html', item_id=item_id, result=result, error=error), code


@login_required
@editor_required
def api_run_activity_review(item_id):
    try:
        result = _run_registered_review(item_id, _api_body())
        return jsonify(result), _review_result_status(result)
    except ReviewError as error:
        return jsonify(error=str(error)), error.status_code
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        return jsonify(error=f'核查流程未能完整完成：{error}。请先检查保留的证据与付费尝试记录，不得自动重试。'), 503


def _private_console_headers(response):
    if request.path.startswith('/console/'):
        response.headers['Cache-Control'] = 'private, no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


def register(app):
    app.context_processor(_queue_template_context)
    app.after_request(_private_console_headers)
    app.add_url_rule('/console/activities', 'reader_review_activities', activities)
    app.add_url_rule('/console/documents', 'reader_review_documents', documents)
    app.add_url_rule('/console/documents/<document_id>/pdf', 'reader_review_document_pdf', document_pdf)
    app.add_url_rule('/console/activities/new', 'reader_review_new', register_activity, methods=['GET', 'POST'])
    app.add_url_rule('/console/activities/<item_id>/review', 'reader_review_run', run_activity_review, methods=['POST'])
    app.add_url_rule('/console/activities/<item_id>/reviews', 'reader_review_reviews', reviews, methods=['GET', 'POST'])
    app.add_url_rule('/console/activities/<item_id>/reviews/<review_id>', 'reader_review_trace', review)
    app.add_url_rule('/console/activities/<item_id>/reviews/<review_id>/artifacts/<role>',
                     'reader_review_artifact', review_artifact)
    app.add_url_rule('/console/activities/<item_id>/reviews/<review_id>/images/<int:image_index>',
                     'reader_review_prepared_image', review_image)
    app.add_url_rule('/console/activities/<item_id>/reviews/<review_id>/sources/<snapshot_id>',
                     'reader_review_source', review_source)
    app.add_url_rule('/console/api/activities', 'reader_review_api_activities', api_activities, methods=['GET', 'POST'])
    app.add_url_rule('/console/api/activities/<item_id>', 'reader_review_api_activity', api_activity, methods=['GET', 'PUT'])
    app.add_url_rule('/console/api/activities/<item_id>/review', 'reader_review_api_run', api_run_activity_review,
                     methods=['POST'])
    app.add_url_rule('/console/api/activities/<item_id>/reviews', 'reader_review_api_reviews', api_reviews,
                     methods=['GET', 'POST'])
    app.add_url_rule('/console/api/activities/<item_id>/reviews/<review_id>', 'reader_review_api_trace', api_review)
    app.add_url_rule('/console/api/activities/<item_id>/reviews/<review_id>/sources/<snapshot_id>',
                     'reader_review_api_source', api_source)
    app.add_url_rule('/console/api/activities/<item_id>/reviews/<review_id>/artifacts/<role>',
                     'reader_review_api_artifact', review_artifact)
    app.add_url_rule('/console/api/activities/<item_id>/reviews/<review_id>/images/<int:image_index>',
                     'reader_review_api_prepared_image', review_image)
    app.add_url_rule('/console/api/publish', 'reader_review_api_publish', api_publish, methods=['POST'])
    app.add_url_rule('/console/activities/<item_id>', 'reader_review_activity', activity, methods=['GET', 'POST'])
    app.add_url_rule('/console/activities/<item_id>/images/<image_id>', 'reader_review_image', image)
    app.add_url_rule('/console/publish', 'reader_review_publish', publish, methods=['POST'])
