"""Reader boundary tests use the real store; only the PDF compiler is replaced."""
from __future__ import annotations

from contextlib import closing
import base64
import json
import sqlite3
import subprocess
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from flask import template_rendered
from werkzeug.datastructures import MultiDict

from test_public_reader import PRIVATE_DTO_FIELDS, reader
from nannanews import daily_acquisition
from nannanews import information
from nannanews import information_reader
from nannanews import runtime


@pytest.fixture
def library(reader):
    with closing(runtime.connect_db()) as conn, conn:
        conn.executemany("INSERT INTO users(username,password,role) VALUES(?,?,?)",
                         [(role, "unused-password-hash", role) for role in ("admin", "editor", "member")])
    reader.register_blueprint(information_reader.bp)
    from nannanews.web import reader_review
    reader_review.register(reader)
    reader.add_url_rule("/logout", endpoint="logout", view_func=lambda: "logout", methods=["POST"])
    reader.add_url_rule("/workbench", endpoint="main", view_func=lambda: "workbench")
    for source_id, college in (("science", "物理学院"), ("arts", "文学院")):
        information.register_source({"id": source_id, "name": source_id, "url": f"https://example.org/{source_id}/",
                                     "publisher_college": college, "status": "verified", "enabled": True})
    return reader


def login(client, username="member"):
    with client.session_transaction() as session:
        session["username"] = username
        session["csrf_token"] = "reader-csrf"


def capture(app, *, entry_id=None, source="science", source_date="2026-10-10", event_date=None,
            deadline_date=None, college="物理学院", relation="associated", text="完整原文中的量子信息。",
            title="校园信息", description="编辑说明", status="pending"):
    source_url = f"https://example.org/{source}/article/{quote(str(entry_id if entry_id is not None else title), safe='')}"
    document = information.save_document({
        "canonical_url": source_url, "title": title, "text": text, "raw_content": text,
        "media": [], "images": [], "source_type": "public_html",
        "fetched_at": "2026-10-11T02:00:00+00:00", "source_published_date": source_date,
    }, source_id=source, actor="editor")
    if entry_id is None:
        return next(record["id"] for record in information.query_records(information.parse_filters({}))["records"]
                    if record["document_id"] == document["id"])
    app.add_entry(entry_id, title=title, description=description, status=status, publish_date=date.today().isoformat())
    record = information.get_entry_record(entry_id)
    with closing(runtime.connect_db()) as conn, conn:
        conn.execute("UPDATE information_records SET document_id=?,event_date=?,deadline_date=?,model_status='completed',evidence_quotes_json=? WHERE id=?",
                     (document["id"], event_date, deadline_date,
                      json.dumps([{"text": "private-model-evidence", "image_index": None}]), record["id"]))
        conn.execute("UPDATE source_documents SET model_status='completed' WHERE id=?", (document["id"],))
        if college:
            conn.execute("INSERT INTO information_colleges(record_id,name,relation,evidence) VALUES(?,?,?,?)",
                         (record["id"], college, relation, "private-college-evidence"))
    return record["id"]


@pytest.mark.parametrize("path", ["/library", "/library/1", "/library/sources", "/api/library", "/api/library/1"])
def test_private_paths_require_real_database_user(library, path):
    client = library.test_client()
    assert client.get(path).status_code == 401
    login(client, "nonexistent-account")
    with client.session_transaction() as session:
        session["role"] = "admin"
    response = client.get(path)
    assert response.status_code == 401
    assert response.headers["Cache-Control"] == "private, no-store"


@pytest.mark.parametrize("role", ["member", "editor", "admin"])
def test_all_real_roles_read_unapproved_records_without_publishing(library, role):
    record_id = capture(library, entry_id=1)
    client = library.test_client()
    login(client, role)
    response = client.get(f"/api/library/{record_id}")
    assert response.status_code == 200
    assert response.get_json()["verification_status"] == "pending"
    assert response.get_json()["content_text"] == "完整原文中的量子信息。"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert client.get("/news/1").status_code == 404
    assert client.get("/api/entries/1").status_code == 404
    assert client.get("/library").status_code == 200


def test_deleted_user_session_cannot_continue_reading(library):
    client = library.test_client()
    login(client)
    assert client.get("/api/library").status_code == 200
    with closing(runtime.connect_db()) as conn, conn:
        conn.execute("DELETE FROM users WHERE username='member'")
    assert client.get("/api/library").status_code == 401


def test_member_text_view_excludes_editorial_evidence(library):
    record_id = capture(library, entry_id=2)
    client = library.test_client()
    login(client)
    member = client.get(f"/api/library/{record_id}").get_json()
    assert "evidence_quotes" not in member
    assert all("evidence" not in college for college in member["colleges"])
    login(client, "editor")
    editor = client.get(f"/api/library/{record_id}").get_json()
    assert editor["evidence_quotes"][0]["text"] == "private-model-evidence"
    assert any(college.get("evidence") == "private-college-evidence" for college in editor["colleges"])


def test_keyword_dates_college_relation_and_source_compose_without_deleting(library):
    wanted = capture(library, entry_id=10, event_date="2026-10-20", deadline_date="2026-10-15")
    capture(library, entry_id=11, event_date="2026-10-21", source="arts")
    capture(library, entry_id=12, event_date="2026-10-22", college="化学学院")
    capture(library, entry_id=13, event_date="2026-11-20")
    capture(library, entry_id=14, event_date=None)
    client = library.test_client()
    login(client)
    query = MultiDict([("q", "量子"), ("date_type", "event"), ("date_from", "2026-10-20"),
                       ("date_to", "2026-10-25"), ("college", "物理学院"), ("college", "天文学院"),
                       ("college_relation", "associated"), ("source", "science")])
    result = client.get("/api/library", query_string=query).get_json()
    assert [record["id"] for record in result["records"]] == [wanted]
    assert result["filters"]["colleges"] == ["物理学院", "天文学院"]
    assert result["filters"]["sources"] == ["science"]
    assert client.get("/api/library").get_json()["total"] == 5
    query.setlist("college_relation", ["publisher"])
    publisher = client.get("/api/library", query_string=query).get_json()
    assert {record["entry_id"] for record in publisher["records"]} == {10, 12}
    query.setlist("date_type", ["deadline"])
    assert client.get("/api/library", query_string=query).get_json()["total"] == 0


@pytest.mark.parametrize("filters", [
    {"date_from": "2026-02-30"}, {"date_from": "2026-10-20", "date_to": "2026-10-10"},
    {"date_type": "uploaded"}, {"college_relation": "recommended"},
])
def test_invalid_filters_fail_explicitly(library, filters):
    client = library.test_client()
    login(client)
    response = client.get("/api/library", query_string=filters)
    assert response.status_code == 400
    assert response.get_json()["error"]
    assert client.get("/library", query_string=filters).status_code == 400


def test_raw_unknown_dates_full_text_and_pagination_are_visible(library):
    raw = capture(library, title="未提取原文", source_date=None, text="第一行\n<script>不能执行</script>完整原文")
    for number in range(20, 23):
        capture(library, entry_id=number)
    client = library.test_client()
    login(client)
    all_records = client.get("/api/library?per_page=2&page=999").get_json()
    assert (all_records["page"], all_records["pages"], all_records["total"]) == (2, 2, 4)
    raw_record = client.get(f"/api/library/{raw}").get_json()
    assert raw_record["entry_id"] is None
    assert raw_record["source_published_date"] is None
    assert raw_record["model_status"] == "not_extracted"
    assert raw_record["description_kind"] == "original_text"
    html = client.get(f"/library/{raw}").get_data(as_text=True)
    assert "&lt;script&gt;不能执行&lt;/script&gt;" in html
    assert "<script>不能执行</script>" not in html
    ranged = client.get("/api/library?date_from=2026-10-01&date_to=2026-10-31").get_json()
    assert raw not in {record["id"] for record in ranged["records"]}


def test_public_metadata_filters_never_expose_private_scope_or_raw_text(library):
    capture(library, entry_id=30, status="approved", event_date="2026-10-20", text="only-private-raw-token")
    capture(library, entry_id=31, status="pending", event_date="2026-10-20")
    capture(library, title="raw-only-private", source_date="2026-10-10")
    client = library.test_client()
    query = {"scope": "private", "date_type": "event", "date_from": "2026-10-20", "date_to": "2026-10-20",
             "source": "science", "college": "物理学院", "college_relation": "publisher"}
    result = client.get("/api/entries", query_string=query).get_json()
    assert [entry["id"] for entry in result["entries"]] == [30]
    dto = result["entries"][0]
    assert dto["event_date"] == "2026-10-20"
    assert dto["source_id"] == "science"
    assert not PRIVATE_DTO_FIELDS.intersection(dto)
    assert all("evidence" not in college for college in dto["colleges"])
    assert "private-model-evidence" not in json.dumps(result)
    assert client.get("/api/entries?q=only-private-raw-token").get_json()["total"] == 0
    assert client.get("/api/entries/31?scope=private").status_code == 404
    assert client.get("/", query_string=query).status_code == 200


def test_sources_display_separate_mode_runs_and_existing_coverage(library, monkeypatch):
    information.register_source({"id": "unsupported", "name": "No feed", "url": "https://example.org/old-article",
                                 "status": "unsupported", "enabled": False, "message": "No discoverable article list"})
    source_run = {"source_id": "science", "status": "partial", "message": "bounded new queue remains",
                  "discovered": 7, "fetched": 2, "extracted": 1, "skipped": 0, "failed": 0,
                  "pending": 1, "last_success_at": None}
    new_run = {"id": 1, "status": "partial", "started_at": "2026-10-10", "finished_at": "2026-10-10"}
    history_run = {"id": 2, "status": "running", "started_at": "2026-10-11", "finished_at": None}
    legacy_run = {"id": 3, "status": "partial", "started_at": "2026-10-09", "finished_at": "2026-10-09"}
    statuses = {
        "new": {"mode": "new", "last_run": new_run, "last_runs": {"new": new_run, "history": history_run, "legacy": legacy_run},
                "sources": [source_run], "pending": 1, "coverage_gaps": ["new-mode gap"]},
        "history": {"mode": "history", "last_run": history_run, "last_runs": {"new": new_run, "history": history_run, "legacy": legacy_run},
                    "sources": [], "pending": 42, "coverage_gaps": ["history-mode gap"]},
    }
    monkeypatch.setattr(daily_acquisition, "get_status", lambda mode="new": statuses[mode])
    monkeypatch.setattr(daily_acquisition, "list_items", lambda **kwargs: {
        "items": [], "total": 0, "page": 1, "pages": 0, "per_page": kwargs["per_page"],
    })
    contexts = []
    def received(sender, template, context, **extra):
        contexts.append(context)
    client = library.test_client()
    login(client)
    with template_rendered.connected_to(received, library):
        response = client.get("/library/sources")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "private, no-store"
    context = contexts[-1]
    assert context["status"]["pending"] == context["new_status"]["pending"] == 1
    assert context["history_status"]["pending"] == 42
    assert context["new_run"]["id"] == 1
    assert context["history_run"]["id"] == 2
    assert context["legacy_run"]["id"] == 3
    assert context["status"]["coverage_gaps"] == ["new-mode gap"]
    sources = {source["id"]: source for source in context["sources"]}
    assert sources["unsupported"]["status"] == "unsupported"
    assert not sources["unsupported"]["enabled"]
    assert sources["science"]["collection"]["pending"] == 1
    assert sources["science"]["collection"]["extracted"] == 1

def test_history_ledger_displays_outcomes_filters_and_preserves_pagination(library, monkeypatch):
    record_id = capture(library, entry_id=901, status="approved", title="Public record", text="Public original")
    rows = [
        {"id": 1, "source_id": "science", "source_name": "science", "title": "Extracted activity",
         "url": "https://example.org/extracted", "document_id": 1, "record_id": record_id,
         "state": "done", "processing_status": "completed", "processing_label": "提取成功",
         "processed_at": "2026-10-12T00:00:00+08:00", "checked_at": "2026-10-15T00:00:00+08:00",
         "message": "", "is_history": True},
        {"id": 2, "source_id": "science", "source_name": "science", "title": "Raw-only activity",
         "url": "https://example.org/raw-only", "document_id": 2, "record_id": None,
         "state": "history_deferred", "processing_status": "history_deferred", "processing_label": "原文留存成功、未提取",
         "processed_at": "2026-10-12T00:01:00+08:00", "checked_at": "2026-10-12T00:01:00+08:00",
         "message": "原文已保存，未调用模型。", "is_history": True},
        {"id": 3, "source_id": "science", "source_name": "science", "title": "Capture failed before document",
         "url": "javascript:private-ledger-token", "document_id": None, "record_id": None,
         "state": "fetch_failed", "processing_status": "fetch_failed", "processing_label": "读取失败",
         "processed_at": "2026-10-12T00:02:00+08:00", "checked_at": "2026-10-12T00:02:00+08:00",
         "message": "private-ledger-reason", "is_history": True},
        {"id": 4, "source_id": "science", "source_name": "science", "title": "Second capture failure",
         "url": "https://example.org/fetch-failed", "document_id": None, "record_id": None,
         "state": "fetch_failed", "processing_status": "fetch_failed", "processing_label": "读取失败",
         "processed_at": "2026-10-12T00:03:00+08:00", "checked_at": "2026-10-12T00:03:00+08:00",
         "message": "capture failed", "is_history": True},
        {"id": 5, "source_id": "science", "source_name": "science", "title": "Model failure",
         "url": "https://example.org/model-failed", "document_id": 3, "record_id": None,
         "state": "model_failed", "processing_status": "model_failed", "processing_label": "提取失败",
         "processed_at": "2026-10-12T00:04:00+08:00", "checked_at": "2026-10-12T00:04:00+08:00",
         "message": "model failed", "is_history": True},
        {"id": 6, "source_id": "science", "source_name": "science", "title": "Uncertain model result",
         "url": "https://example.org/uncertain", "document_id": 4, "record_id": None,
         "state": "model_uncertain", "processing_status": "model_uncertain", "processing_label": "结果未确认",
         "processed_at": "2026-10-12T00:05:00+08:00", "checked_at": "2026-10-12T00:05:00+08:00",
         "message": "result needs manual review", "is_history": True},
        {"id": 7, "source_id": "science", "source_name": "science", "title": "Pending activity",
         "url": "https://example.org/pending", "document_id": None, "record_id": None,
         "state": "pending", "processing_status": "pending", "processing_label": "等待处理",
         "processed_at": None, "checked_at": None, "message": "", "is_history": True},
        {"id": 8, "source_id": "science", "source_name": "science", "title": "Running extraction",
         "url": "https://example.org/running", "document_id": 5, "record_id": None,
         "state": "modelling", "processing_status": "running", "processing_label": "正在提取",
         "processed_at": None, "checked_at": "2026-10-12T00:06:00+08:00", "message": "", "is_history": True},
    ]
    run = {"id": 1, "status": "completed", "started_at": "2026-10-12", "finished_at": "2026-10-12"}
    statuses = {
        mode: {"mode": mode, "last_run": run if mode == "history" else None,
               "last_runs": {"new": None, "history": run, "legacy": None},
               "sources": [], "pending": 0, "coverage_gaps": []}
        for mode in ("new", "history")
    }
    calls = []

    def list_items(*, mode, source_id, state, page, per_page):
        calls.append({"mode": mode, "source_id": source_id, "state": state, "page": page, "per_page": per_page})
        selected = [row for row in rows if (source_id is None or row["source_id"] == source_id)
                    and (state is None or row["state"] == state)]
        total = len(selected)
        pages = (total + per_page - 1) // per_page
        start = (page - 1) * per_page
        return {"items": selected[start:start + per_page], "total": total, "page": page,
                "pages": pages, "per_page": per_page}

    monkeypatch.setattr(daily_acquisition, "get_status", lambda mode="new": statuses[mode])
    monkeypatch.setattr(daily_acquisition, "list_items", list_items)
    client = library.test_client()
    login(client)

    first = client.get("/library/sources?mode=history&source_id=science&per_page=2")
    first_html = first.get_data(as_text=True)
    assert first.status_code == 200
    assert "提取成功" in first_html
    assert 'href="https://example.org/extracted"' in first_html
    assert "2026-10-12T00:00:00+08:00" in first_html
    assert "2026-10-15T00:00:00+08:00" not in first_html
    assert "原文留存成功、未提取" in first_html
    assert f"/library/{record_id}" in first_html
    assert "mode=history&amp;source_id=science&amp;per_page=2&amp;page=2" in first_html

    second = client.get("/library/sources?mode=history&source_id=science&per_page=2&page=2")
    second_html = second.get_data(as_text=True)
    assert "读取失败" in second_html and "提取失败" in second_html
    assert "private-ledger-reason" in second_html
    assert "javascript:" not in second_html

    third = client.get("/library/sources?mode=history&source_id=science&per_page=2&page=3")
    assert "结果未确认" in third.get_data(as_text=True)
    fourth = client.get("/library/sources?mode=history&source_id=science&per_page=2&page=4")
    fourth_html = fourth.get_data(as_text=True)
    assert "等待处理" in fourth_html and "正在提取" in fourth_html
    assert "尚未完成" in fourth_html

    filtered = client.get("/library/sources?mode=history&source_id=science&state=fetch_failed&page=2&per_page=1")
    filtered_html = filtered.get_data(as_text=True)
    assert "Second capture failure" in filtered_html
    assert "mode=history&amp;source_id=science&amp;state=fetch_failed&amp;per_page=1&amp;page=1" in filtered_html
    assert calls[-1] == {"mode": "history", "source_id": "science", "state": "fetch_failed", "page": 2, "per_page": 1}

    public_result = client.get("/api/entries").get_json()
    assert "private-ledger-reason" not in json.dumps(public_result)
    assert "原文留存成功，未提取" not in json.dumps(public_result)


@pytest.mark.parametrize("query", [
    {"mode": "legacy"}, {"state": "private"}, {"page": "0"}, {"per_page": "51"},
    MultiDict([("mode", "new"), ("mode", "history")]),
])
def test_source_ledger_rejects_invalid_query_values(library, query):
    client = library.test_client()
    login(client)
    response = client.get("/library/sources", query_string=query)
    assert response.status_code == 400
    assert response.headers["Cache-Control"] == "private, no-store"


def test_source_page_without_daily_schema_is_read_only_and_truthful(library):
    daily_tables = {"daily_runs", "daily_source_runs", "daily_links", "daily_pages", "daily_source_state"}
    with closing(runtime.connect_db()) as conn, conn:
        before_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        before_counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                         for name in daily_tables.intersection(before_tables)}
    client = library.test_client()
    login(client)
    response = client.get("/library/sources")
    assert response.status_code == 200
    with closing(runtime.connect_db()) as conn, conn:
        after_tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        after_counts = {name: conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
                        for name in daily_tables.intersection(after_tables)}
    assert before_tables == after_tables
    assert before_counts == after_counts

def test_source_status_storage_error_is_not_reported_as_success(library, monkeypatch):
    def unavailable(mode="new"):
        raise sqlite3.OperationalError("private database failure detail")
    monkeypatch.setattr(daily_acquisition, "get_status", unavailable)
    client = library.test_client()
    login(client)
    response = client.get("/library/sources")
    assert response.status_code == 503
    assert response.headers["Cache-Control"] == "private, no-store"
    assert "当前处理结果未确认" in response.get_data(as_text=True)
    assert "private database failure detail" not in response.get_data(as_text=True)

def test_export_uses_entire_matching_result_not_page_and_cleans_output(library, monkeypatch):
    for number in range(100, 125):
        capture(library, entry_id=number, event_date="2026-10-20")
    capture(library, entry_id=200, source="arts", event_date="2026-10-20")
    compiled = []
    def compile_pdf(records, filters, pdf_path):
        compiled.append((records, filters, Path(pdf_path)))
        pdf_path.write_bytes(b"%PDF-private-matching-records")
        return subprocess.CompletedProcess([], 0)
    monkeypatch.setattr(information_reader.publication, "compile_information_pdf", compile_pdf)
    client = library.test_client()
    login(client)
    response = client.post("/library/export", data={"csrf_token": "reader-csrf", "source": "science",
                           "q": "量子", "date_type": "event", "date_from": "2026-10-20", "date_to": "2026-10-20",
                           "college": "物理学院", "college_relation": "publisher", "page": "2", "per_page": "2"})
    assert response.status_code == 200
    assert response.data == b"%PDF-private-matching-records"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert response.mimetype == "application/pdf"
    assert len(compiled) == 1
    assert {record["entry_id"] for record in compiled[0][0]} == set(range(100, 125))
    assert compiled[0][1]["sources"] == ["science"]
    assert compiled[0][1]["college_relation"] == "publisher"
    assert not compiled[0][2].exists()
    assert not list((runtime.settings.publication_dir / "exports").iterdir())
    response.close()


def test_export_requires_auth_post_and_csrf_before_compiler(library, monkeypatch):
    calls = []
    monkeypatch.setattr(information_reader.publication, "compile_information_pdf", lambda *args: calls.append(args))
    client = library.test_client()
    assert client.post("/library/export", data={"csrf_token": "reader-csrf"}).status_code == 401
    login(client)
    assert client.get("/library/export").status_code == 405
    assert client.post("/library/export").status_code == 400
    assert client.post("/library/export", data={"csrf_token": "wrong"}).status_code == 400
    assert calls == []


def test_export_over_limit_rejects_instead_of_truncating(library, monkeypatch):
    for number in range(1, 502):
        library.add_entry(number, status="pending", publish_date=None)
    calls = []
    monkeypatch.setattr(information_reader.publication, "compile_information_pdf", lambda *args: calls.append(args))
    client = library.test_client()
    login(client)
    response = client.post("/library/export", data={"csrf_token": "reader-csrf"})
    assert response.status_code == 400
    assert calls == []
    assert client.get("/api/library").get_json()["total"] == 501
    with closing(runtime.connect_db()) as conn, conn:
        conn.execute("DELETE FROM entries WHERE id=501")
    def compile_pdf(records, filters, pdf_path):
        calls.append(records)
        pdf_path.write_bytes(b"%PDF-exact-limit")
        return subprocess.CompletedProcess([], 0)
    monkeypatch.setattr(information_reader.publication, "compile_information_pdf", compile_pdf)
    response = client.post("/library/export", data={"csrf_token": "reader-csrf"})
    assert response.status_code == 200
    assert len(calls[0]) == 500
    response.close()


@pytest.mark.parametrize("failure", ["nonzero", "missing", "exception"])
def test_export_failures_are_safe_and_remove_temporary_output(library, monkeypatch, failure):
    capture(library, entry_id=400)
    def compile_pdf(records, filters, pdf_path):
        if failure == "exception":
            raise RuntimeError("private-compiler-error-key")
        if failure == "nonzero":
            pdf_path.write_bytes(b"partial-output")
        return subprocess.CompletedProcess([], 1 if failure == "nonzero" else 0, stderr="private-compiler-error-key")
    monkeypatch.setattr(information_reader.publication, "compile_information_pdf", compile_pdf)
    client = library.test_client()
    login(client)
    response = client.post("/library/export", data={"csrf_token": "reader-csrf"})
    assert response.status_code == 503
    assert "private-compiler-error-key" not in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "private, no-store"
    assert not list((runtime.settings.publication_dir / "exports").iterdir())


def test_captured_image_evidence_never_becomes_public_or_member_media(library):
    image = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aJp0AAAAASUVORK5CYII=")
    document = information.save_document({
        "canonical_url": "https://example.org/private-image-evidence", "title": "Captured image", "text": "Original image article",
        "raw_content": "Original image article", "media": [{"data": base64.b64encode(image).decode(), "url": "https://example.org/picture.png"}],
        "images": [], "source_type": "public_html",
    }, source_id="science", actor="editor")
    record = next(record for record in information.query_records(information.parse_filters({}))["records"]
                  if record["document_id"] == document["id"])
    url = f"/library/{record['id']}/images/0"
    client = library.test_client()
    assert client.get(url).status_code == 401
    login(client)
    assert client.get(url).status_code == 403
    login(client, "editor")
    response = client.get(url)
    assert response.status_code == 200
    assert response.data == image
    assert response.mimetype == "image/png"
    assert response.headers["Cache-Control"] == "private, no-store"
    assert client.get(f"/library/{record['id']}/images/1").status_code == 404
    assert client.get("/api/entries").get_json()["total"] == 0


def test_pagination_links_preserve_repeated_filters_and_page_size(library):
    for number in range(600, 603):
        capture(library, entry_id=number, event_date="2026-10-20")
    contexts = []
    def received(sender, template, context, **extra):
        contexts.append(context)
    query = MultiDict([("q", "量子"), ("date_type", "event"), ("date_from", "2026-10-20"),
                       ("date_to", "2026-10-20"), ("college", "物理学院"), ("college", "天文学院"),
                       ("college_relation", "associated"), ("source", "science"), ("source", "arts"),
                       ("tag", "校级活动"), ("per_page", "1")])
    client = library.test_client()
    login(client)
    with template_rendered.connected_to(received, library):
        response = client.get("/library", query_string=query)
    assert response.status_code == 200
    context = contexts[-1]
    assert context["total"] == 3 and context["pages"] == 3
    next_query = parse_qs(urlsplit(context["next_url"]).query)
    assert next_query["page"] == ["2"]
    for key in query:
        assert next_query[key] == query.getlist(key)


def test_empty_export_never_calls_compiler(library, monkeypatch):
    calls = []
    monkeypatch.setattr(information_reader.publication, "compile_information_pdf", lambda *args: calls.append(args))
    client = library.test_client()
    login(client)
    response = client.post("/library/export", data={"csrf_token": "reader-csrf", "q": "absent"})
    assert response.status_code == 400
    assert calls == []


def test_public_invalid_filter_api_returns_error_contract(library):
    response = library.test_client().get("/api/entries?date_type=uploaded")
    assert response.status_code == 400
    assert response.is_json and response.get_json()["error"]


@pytest.mark.parametrize("date_type, exact_date", [("source", "2026-10-10"), ("event", "2026-10-20"), ("deadline", "2026-10-15")])
def test_date_kinds_use_their_own_metadata_and_legacy_exact_date(library, date_type, exact_date):
    record_id = capture(library, entry_id=700, event_date="2026-10-20", deadline_date="2026-10-15")
    client = library.test_client()
    login(client)
    query = {"date_type": date_type, "date": exact_date, "source": "science",
             "college": "物理学院", "college_relation": "publisher"}
    result = client.get("/api/library", query_string=query).get_json()
    assert [record["id"] for record in result["records"]] == [record_id]
    assert result["filters"]["date_from"] == exact_date == result["filters"]["date_to"]
    query["date"] = "2026-10-01"
    assert client.get("/api/library", query_string=query).get_json()["total"] == 0
    publication = client.get("/api/library", query_string={"date_type": "publication", "date": date.today().isoformat()}).get_json()
    assert [record["id"] for record in publication["records"]] == [record_id]


def test_failed_extraction_keeps_actual_original_queryable(library):
    record_id = capture(library, title="Failed extraction original", source_date=None, text="尚未提取成功的完整原文")
    record = information.get_record(record_id)
    with closing(runtime.connect_db()) as conn, conn:
        conn.execute("UPDATE source_documents SET model_status='failed',model_error='model_response_invalid' WHERE id=?", (record["document_id"],))
    client = library.test_client()
    login(client)
    result = client.get("/api/library", query_string={"q": "原文"}).get_json()
    assert result["total"] == 1
    visible = result["records"][0]
    assert visible["id"] == record_id and visible["entry_id"] is None
    assert visible["model_status"] == "failed" and visible["verification_status"] == "unverified"
    assert visible["content_text"] == "尚未提取成功的完整原文"
    assert visible["event_date"] is None and visible["deadline_date"] is None
    assert "model_error" not in visible
    assert client.get("/api/entries").get_json()["total"] == 0
    detail = client.get(f"/library/{record_id}").get_data(as_text=True)
    assert "提取失败" in detail
    assert "人工审核状态" in detail
    assert "来源处理台账" in detail
    assert "model_response_invalid" not in detail

def test_detail_never_labels_raw_only_activity_as_model_success(library):
    record_id = capture(library, entry_id=902, title="Raw-only record")
    with closing(runtime.connect_db()) as conn, conn:
        conn.execute("UPDATE information_records SET model_status='history_deferred' WHERE id=?", (record_id,))
    client = library.test_client()
    login(client)
    detail = client.get(f"/library/{record_id}").get_data(as_text=True)
    assert "原文留存成功，未提取" in detail
    assert "提取成功" not in detail
    assert "人工审核状态" in detail
    assert "mode=history&amp;source_id=science" in detail
