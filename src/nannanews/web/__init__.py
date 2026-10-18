"""Flask application factory; importing this package never initializes state."""

from datetime import timedelta

from flask import Flask

from nannanews import acquisition, information_reader, public_reader, runtime, schema
from . import administration, auth, editorial, intake, publication, security


def create_app():
    """Require explicit migrations, then prepare private runtime and register routes."""
    schema.require_schema()
    runtime.prepare_runtime()
    app = Flask(
        __name__,
        template_folder=str(runtime.CODE_DIR / 'templates'),
        static_folder=str(runtime.CODE_DIR / 'static'),
        static_url_path='/static',
    )
    app.secret_key = runtime.settings.session_secret
    app.permanent_session_lifetime = timedelta(days=7)
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=runtime.settings.session_cookie_secure,
        MAX_CONTENT_LENGTH=16 * 1024 * 1024,
    )
    security.register(app)
    auth.register(app)
    intake.register(app)
    editorial.register(app)
    administration.register(app)
    publication.register(app)
    acquisition.register(app)
    app.register_blueprint(public_reader.bp)
    app.register_blueprint(information_reader.bp)
    return app
