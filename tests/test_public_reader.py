from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from flask import Flask, render_template


from nannanews import public_reader
from nannanews import runtime, schema


EXPECTED_DTO_FIELDS = {
    "id", "information_id", "title", "description", "source_url", "tag", "due_time",
    "publish_date", "lecture_data", "image_url", "source_id", "source_name",
    "source_published_date", "event_date", "deadline_date", "publication_date",
    "verification_status", "colleges",
}
PRIVATE_DTO_FIELDS = {"content_text", "document_id", "evidence_quotes", "model_status",
                      "model_error", "date_uncertainty", "captured_by", "document_json"}


@pytest.fixture

def reader(tmp_path, monkeypatch):
    database = tmp_path / "reader.sqlite"
    uploads = tmp_path / "uploads"
    archives = tmp_path / "archives"
    catalog = tmp_path / "source_catalog.json"
    monkeypatch.setattr(runtime, "settings", runtime.Settings(
        data_dir=tmp_path, database_path=database, upload_dir=uploads, archive_dir=archives,
        publication_dir=tmp_path / "publications", lecture_archive_dir=tmp_path / "lecture_archives",
        session_secret="test-only-session-secret",
    ))
    schema.migrate()

    app = Flask(__name__, template_folder=str(runtime.CODE_DIR / "templates"), static_folder=str(runtime.CODE_DIR / "static"))
    app.secret_key = "test-only-session-secret"
    app.register_blueprint(public_reader.bp)
    app.context_processor(lambda: {"csrf_token": lambda: "test-csrf-token"})
    app.add_url_rule('/login', endpoint='login', view_func=lambda: 'Editor login')

    def add_entry(entry_id, *, title="Public event", description="Public details", status="approved", publish_date=None,
                  tag="校级活动", link="https://events.example.org/item", due_time=None, lecture_data=None,
                  use_image=0, entry_type="活动预告"):
        with sqlite3.connect(database) as conn:
            conn.execute("""INSERT INTO entries
                (id, uploader, upload_time, title, link, description, describer, reviewer, due_time,
                 status, type, locked_by, lock_time, use_image, publish_date, tag, short_title,
                 lecture_data, priority)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (entry_id, "private-uploader", "2026-01-01 10:00:00", title, link, description,
                 "private-describer", "private-reviewer", due_time, status, entry_type,
                 "private-lock-owner", "private-lock-time", use_image, publish_date, tag,
                 "private-short-title", lecture_data, 999))

    app.add_entry = add_entry
    app.test_uploads = uploads
    app.test_archives = archives
    app.test_catalog = catalog
    return app


def test_json_lists_only_approved_valid_published_entries_and_named_fields(reader):
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    invalid_date = f"{today.year - 1}-02-30"
    yesterday = (today - timedelta(days=1)).isoformat()
    reader.add_entry(1, title="Published", publish_date=yesterday)
    reader.add_entry(2, title="Pending", status="pending", publish_date=yesterday)
    reader.add_entry(3, title="Described", status="described", publish_date=yesterday)
    reader.add_entry(4, title="Unpublished", publish_date=None)
    reader.add_entry(5, title="Epoch", publish_date="1970-01-01 00:00:00")
    reader.add_entry(6, title="Future", publish_date=(today + timedelta(days=1)).isoformat())
    reader.add_entry(7, title="Impossible date", publish_date=invalid_date)

    response = reader.test_client().get("/api/entries")

    assert response.status_code == 200
    payload = response.get_json()
    assert [entry["id"] for entry in payload["entries"]] == [1]
    assert EXPECTED_DTO_FIELDS <= set(payload["entries"][0])
    assert not PRIVATE_DTO_FIELDS.intersection(payload["entries"][0])
    public_body = json.dumps(payload, ensure_ascii=False)
    for private_value in ("private-uploader", "private-describer", "private-reviewer", "private-lock-owner", "private-short-title"):
        assert private_value not in public_body


def test_draft_detail_api_and_pdf_are_not_public_even_if_file_exists(reader):
    today = date.today().isoformat()
    reader.add_entry(10, status="described", publish_date=today)
    (reader.test_archives / f"{today}.pdf").write_bytes(b"private draft PDF")
    client = reader.test_client()

    assert client.get("/news/10").status_code == 404
    assert client.get("/api/entries/10").status_code == 404
    assert client.get(f"/issues/{today}/pdf").status_code == 404

def test_published_images_are_public_but_draft_images_require_editor_role(reader, monkeypatch):
    today = date.today().isoformat()
    image = reader.test_uploads / "source.png"
    image.write_bytes(b"image payload")
    reader.add_entry(11, title="Published image", publish_date=today, link="source.png", use_image=1)
    reader.add_entry(12, title="Draft image", status="described", publish_date=today, link="source.png", use_image=1)
    client = reader.test_client()

    assert client.get("/news/11/image").data == b"image payload"
    assert client.get("/news/12/image").status_code == 404
    assert client.get("/editor/entries/12/image").status_code == 401

    monkeypatch.setattr(runtime, "get_user_role", lambda username: "editor")
    with client.session_transaction() as session:
        session["username"] = "editor-account"
    private_image = client.get("/editor/entries/12/image")
    assert private_image.status_code == 200
    assert private_image.headers["Cache-Control"] == "private, no-store"
    assert private_image.data == b"image payload"


def test_search_treats_wildcards_as_literal_and_public_html_escapes_content(reader):
    today = date.today().isoformat()
    reader.add_entry(20, title="100%_public <script>alert(1)</script>", description="Quoted 'details' & <img src=x onerror=alert(1)>", publish_date=today)
    reader.add_entry(21, title="ordinary public event", publish_date=today)
    client = reader.test_client()

    found = client.get("/api/entries", query_string={"q": "%_"}).get_json()
    assert [entry["id"] for entry in found["entries"]] == [20]
    html = client.get("/news/20").get_data(as_text=True)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert "<script>alert(1)</script>" not in html
    assert "<img src=x onerror=alert(1)>" not in html


def test_source_directory_filters_sensitive_catalog_columns_and_escapes_names(reader):
    reader.test_catalog.write_text(json.dumps([{
        "id": "private-id",
        "name": "Public <Source>",
        "category": "Campus",
        "subcategory": "Notices",
        "unit": "Public unit",
        "account_type": "private-account-type",
        "wechat_id": "private-wechat-id",
        "description": "Public source description",
        "source_urls": ["https://source.example.org/notices", "javascript:alert(1)"],
        "provenance": [{"workbook": "private-workbook.xlsx", "sheet": "Contacts", "row": 4}],
    }], ensure_ascii=False), encoding="utf-8")

    response = reader.test_client().get("/sources")
    html = response.get_data(as_text=True)

    assert response.status_code == 200
    assert "Public &lt;Source&gt;" in html
    assert "https://source.example.org/notices" in html
    for private_value in ("private-id", "private-account-type", "private-wechat-id", "private-workbook.xlsx", "Contacts"):
        assert private_value not in html
    assert "javascript:alert(1)" not in html


def test_lecture_names_with_quotes_render_as_data_in_public_and_editor_views(reader):
    title = "O'Hara \"Quoted\" Talk </script><script>alert(1)</script>"
    lecture_data = {
        "series": {"name": "Researcher's Forum", "number": "第1期"},
        "title": {"primary": title, "secondary": "A speaker's view"},
        "schedule": {"date": date.today().isoformat(), "start_time": "10:00", "end_time": "11:00"},
        "venue": "报告厅",
        "speakers": [{"name": "D'Arcy", "title": "教授", "biography": ""}],
    }
    raw = json.dumps(lecture_data, ensure_ascii=False)
    reader.add_entry(30, title=title, publish_date=date.today().isoformat(), tag="讲座", entry_type="活动预告", lecture_data=raw)
    public_html = reader.test_client().get("/news/30").get_data(as_text=True)
    assert "O&#39;Hara" in public_html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in public_html
    assert "<script>alert(1)</script>" not in public_html


def test_pdf_is_withdrawn_when_published_content_changes(reader):
    today = date.today().isoformat()
    reader.add_entry(40, publish_date=today)
    (reader.test_archives / f'{today}.pdf').write_bytes(b'%PDF-public-snapshot')
    (reader.test_archives / f'{today}.json').write_text(json.dumps({'public_signature': public_reader.publication_signature(today)}))
    client = reader.test_client()
    response = client.get(f'/issues/{today}/pdf')
    assert response.status_code == 200 and response.data == b'%PDF-public-snapshot'
    assert response.headers['Cache-Control'] == 'no-store'
    with public_reader.runtime.connect_db() as conn:
        conn.execute("UPDATE entries SET description='Corrected public content' WHERE id=40")
    assert client.get(f'/issues/{today}/pdf').status_code == 404
    assert client.get('/api/entries/40').get_json()['description'] == 'Corrected public content'


def test_daily_issue_includes_entries_beyond_api_page_limit(reader):
    today = date.today().isoformat()
    for number in range(51):
        reader.add_entry(100 + number, title=f'Boundary event {number:03}', publish_date=today)
    client = reader.test_client()
    listing = client.get('/api/entries?per_page=50').get_json()
    assert listing['total'] == 51 and listing['pages'] == 2
    issue = client.get(f'/issues/{today}').get_data(as_text=True)
    assert all(f'Boundary event {number:03}' in issue for number in range(51))


@pytest.mark.parametrize('value, expected_date, expected_time, expected_zone', [
    ('2026-10-15T23:59:59Z', '2026-10-16', '07:59:59', '北京时间'),
    ('2026-10-15T23:00:00-05:00', '2026-10-16', '12:00:00', '北京时间'),
    ('2026-10-16T04:00:00+08:00', '2026-10-16', '04:00:00', '北京时间'),
    ('2026-10-15 23:59:59', '2026-10-15', '23:59:59', ''),
    ('2026-10-15', '2026-10-15', '', ''),
    (date(2026, 10, 15), '2026-10-15', '', ''),
    (datetime(2026, 10, 15, 23, 59), '2026-10-15', '23:59:00', ''),
])
def test_display_date_preserves_calendar_and_timezone_semantics(value, expected_date, expected_time, expected_zone):
    displayed = public_reader._display_date(value)
    assert displayed['date'] == expected_date
    assert displayed['time'] == expected_time
    assert displayed['zone'] == expected_zone
    if expected_zone:
        assert datetime.fromisoformat(displayed['datetime']).utcoffset() == timedelta(hours=8)
    elif expected_time:
        assert datetime.fromisoformat(displayed['datetime']).tzinfo is None
    else:
        assert displayed['datetime'] == expected_date


@pytest.mark.parametrize('value', [None, '', '未知', '10月15日', '2026-02-30',
                                  '2026-10-15T25:00:00Z', '9999-12-31T23:00:00-10:00', '2' * 65])
def test_display_date_does_not_invent_invalid_or_incomplete_dates(value):
    assert public_reader._display_date(value) is None
