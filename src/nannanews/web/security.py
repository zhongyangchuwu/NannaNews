"""Session authorization and CSRF protection for every controller."""

from functools import wraps
import secrets

from flask import abort, redirect, render_template, request, session, url_for

from nannanews import runtime

def csrf_token():
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_urlsafe(32)
        session['csrf_token'] = token
    return token

def protect_mutations():
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        expected = session.get('csrf_token', '')
        supplied = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token', '')
        if not expected or not secrets.compare_digest(expected.encode('utf-8'), supplied.encode('utf-8')):
            abort(400, description='CSRF token missing or invalid')

def shared_context():
    return {
        'csrf_token': csrf_token,
        'user_role': runtime.get_user_role(session.get('username')) if session.get('username') else None,
        'site_name': runtime.settings.site_name,
    }

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not session.get('username') or runtime.get_user_role(session['username']) is None:
            session.clear()
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if runtime.get_user_role(session.get('username')) != 'admin':
            return abort(401)
        return f(*args, **kwargs)
    return decorated_function

def editor_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if runtime.get_user_role(session.get('username')) not in ('admin', 'editor'):
            return abort(401)
        return f(*args, **kwargs)
    return decorated_function


def not_found(error):
    return render_template('404.html'), 404


def unauthorized(error):
    return render_template('401.html'), 401


def register(app):
    app.before_request(protect_mutations)
    app.context_processor(shared_context)
    app.register_error_handler(404, not_found)
    app.register_error_handler(401, unauthorized)
