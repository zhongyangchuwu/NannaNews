"""Validation shared by intake, editorial forms and publication."""

from datetime import datetime, timedelta, timezone
import re
from urllib.parse import urlparse

from flask import abort

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'webp'}

LINK_REGEX = re.compile(r"(https?:\/\/(?:www\.)?[-a-zA-Z0-9@:%._\+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_\+.~#?&//=]*)?)")

def allowed_file(filename):
    if not filename:
        return False
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def is_valid_url(url):
    try:
        parts = urlparse(url)
        return parts.scheme in ('http', 'https') and bool(parts.hostname) and not parts.username and not parts.password
    except (TypeError, ValueError):
        return False

def valid_date(raw, past_only=False):
    if not isinstance(raw, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', raw):
        abort(400, description='日期须使用 YYYY-MM-DD 格式。')
    try:
        value = datetime.strptime(raw, '%Y-%m-%d').date()
    except ValueError:
        abort(400, description='日期不存在。')
    if raw == '1970-01-01' or (past_only and value > (datetime.now(timezone.utc) + timedelta(hours=8)).date()):
        abort(400, description='不可刊发无效日期或未来刊物。')
    return raw


