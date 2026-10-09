"""AC1-AC3: resume status, upload/overwrite, and rejection of unsupported formats."""

from pathlib import Path

import pytest

from app import auth, config, db, resume_extract, resume_store

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def resume_dir(data_dir, monkeypatch):
    """Redirect the raw-file directory used by the upload router into tmp."""
    d = data_dir / "resume"
    monkeypatch.setattr(config, "RESUME_DIR", d)
    return d


def _hdrs():
    return {"X-App-Token": auth.current_token()}


def _upload(client, fixture_name, *, as_name=None):
    data = (FIXTURES / fixture_name).read_bytes()
    return client.post(
        "/api/resume",
        headers=_hdrs(),
        files={"file": (as_name or fixture_name, data)},
    )


def _stored():
    conn = db.get_connection()
    try:
        rows = conn.execute("SELECT COUNT(*) FROM resume").fetchone()[0]
        return rows, resume_store.get_resume(conn)
    finally:
        conn.close()


# --- AC1 -------------------------------------------------------------------


def test_ac1_status_when_none_uploaded(client, token):
    r = client.get("/api/resume/status", headers=_hdrs())
    assert r.status_code == 200
    assert r.json() == {"uploaded": False, "filename": None, "uploaded_at": None}


def test_ac1_status_requires_auth(client, token):
    assert client.get("/api/resume/status").status_code == 401
    assert client.post("/api/resume").status_code == 401


# --- AC2 -------------------------------------------------------------------


def test_ac2_upload_pdf_then_status_reflects_it(client, token, resume_dir):
    r = _upload(client, "sample.pdf")
    assert r.status_code == 201
    body = r.json()
    assert body["uploaded"] is True
    assert body["filename"] == "sample.pdf"
    assert body["uploaded_at"]

    status = client.get("/api/resume/status", headers=_hdrs()).json()
    assert status == body

    _, rec = _stored()
    assert rec.extractor_version == resume_extract.EXTRACTOR_VERSION
    assert "Acme Corp" in rec.extracted_text
    assert Path(rec.raw_path) == resume_dir / "raw.pdf"
    assert Path(rec.raw_path).read_bytes() == (FIXTURES / "sample.pdf").read_bytes()


def test_ac2_reupload_overwrites(client, token, resume_dir):
    assert _upload(client, "sample.pdf").status_code == 201
    assert _upload(client, "sample.docx").status_code == 201

    status = client.get("/api/resume/status", headers=_hdrs()).json()
    assert status["filename"] == "sample.docx"

    count, rec = _stored()
    assert count == 1
    assert rec.filename == "sample.docx"
    assert "Languages | Python, Go" in rec.extracted_text
    # Exactly one raw file on disk -- the old raw.pdf is gone.
    assert sorted(p.name for p in resume_dir.iterdir()) == ["raw.docx"]


def test_ac2_txt_upload_succeeds_and_replaces(client, token):
    assert _upload(client, "sample.docx").status_code == 201
    r = _upload(client, "sample.txt")
    assert r.status_code == 201
    count, rec = _stored()
    assert count == 1
    assert rec.filename == "sample.txt"
    assert rec.extracted_text.startswith("Jane Doe\nBackend Engineer")


# --- AC3 -------------------------------------------------------------------


def _assert_rejection_message(detail: str):
    assert "PDF" in detail
    assert "DOCX" in detail or "Word" in detail
    assert "TXT" in detail or "text" in detail


def test_ac3_doc_rejected_existing_untouched(client, token, resume_dir):
    assert _upload(client, "sample.pdf").status_code == 201
    before = client.get("/api/resume/status", headers=_hdrs()).json()
    _, rec_before = _stored()

    r = _upload(client, "sample.doc")
    assert r.status_code == 422
    _assert_rejection_message(r.json()["detail"])
    assert "'.doc'" in r.json()["detail"]

    assert client.get("/api/resume/status", headers=_hdrs()).json() == before
    count, rec_after = _stored()
    assert count == 1
    assert rec_after == rec_before
    assert sorted(p.name for p in resume_dir.iterdir()) == ["raw.pdf"]


def test_ac3_png_rejected(client, token, resume_dir):
    assert _upload(client, "sample.docx").status_code == 201
    before = client.get("/api/resume/status", headers=_hdrs()).json()

    r = _upload(client, "sample.png")
    assert r.status_code == 422
    _assert_rejection_message(r.json()["detail"])

    assert client.get("/api/resume/status", headers=_hdrs()).json() == before
    assert sorted(p.name for p in resume_dir.iterdir()) == ["raw.docx"]


def test_ac3_corrupt_pdf_rejected_existing_untouched(client, token, resume_dir):
    """A .png renamed to .pdf passes the extension check but fails extraction;
    it must still be rejected before anything is written."""
    assert _upload(client, "sample.txt").status_code == 201
    _, rec_before = _stored()

    r = _upload(client, "sample.png", as_name="resume.pdf")
    assert r.status_code == 422
    _assert_rejection_message(r.json()["detail"])

    _, rec_after = _stored()
    assert rec_after == rec_before
    assert sorted(p.name for p in resume_dir.iterdir()) == ["raw.txt"]


def test_ac3_validate_extension_unit():
    assert resume_extract.validate_extension("CV.PDF") == ".pdf"
    assert resume_extract.validate_extension("cv.md") == ".md"
    with pytest.raises(resume_extract.UnsupportedFormatError) as exc:
        resume_extract.validate_extension("noextension")
    assert "'(none)'" in exc.value.message


# --- Caching (AC2 support) -------------------------------------------------


def test_extraction_caching_skips_reextract_on_identical_bytes(
    client, token, monkeypatch
):
    assert _upload(client, "sample.pdf").status_code == 201
    _, first = _stored()

    def boom(*_a, **_kw):
        raise AssertionError("extract_text must not be called on a cache hit")

    monkeypatch.setattr(resume_extract, "extract_text", boom)
    r = _upload(client, "sample.pdf", as_name="renamed.pdf")
    assert r.status_code == 201

    _, second = _stored()
    assert second.extracted_text == first.extracted_text
    assert second.content_hash == first.content_hash
    # filename/uploaded_at still update -- only extraction is short-circuited.
    assert second.filename == "renamed.pdf"
    assert second.uploaded_at >= first.uploaded_at


def test_extraction_reruns_when_extractor_version_changes(client, token, monkeypatch):
    assert _upload(client, "sample.pdf").status_code == 201
    calls = []
    real = resume_extract.extract_text

    def spy(b, ext):
        calls.append(ext)
        return real(b, ext)

    monkeypatch.setattr(resume_extract, "EXTRACTOR_VERSION", "v2-test")
    monkeypatch.setattr(resume_extract, "extract_text", spy)
    assert _upload(client, "sample.pdf").status_code == 201
    assert calls == [".pdf"]
    _, rec = _stored()
    assert rec.extractor_version == "v2-test"


# --- Extraction unit tests ---------------------------------------------------


def test_clean_text_rejoins_hyphenation_and_strips_page_numbers():
    raw = "Built soft-\nware systems\n3\nNext   line\twith\t\ttabs\n\n\n\n\n\nEnd"
    out = resume_extract.clean_text(raw)
    assert "software" in out
    assert "soft-" not in out
    assert "\n3\n" not in out and not any(
        line.strip() == "3" for line in out.split("\n")
    )
    assert "Next line with tabs" in out
    assert "\n\n\n\n" not in out
    assert out.endswith("End")


def test_clean_text_nfkc_normalizes():
    assert resume_extract.clean_text("ﬁnance") == "finance"  # "fi" ligature


def test_extract_pdf_column_aware():
    text = resume_extract.extract_text((FIXTURES / "sample.pdf").read_bytes(), ".pdf")
    # "Owned the billing..." is the LAST left-column line (y=350); "SKILLS" is the
    # first right-column line and sits visually HIGHER (y=120). Column-aware
    # extraction must still emit the whole left column first.
    assert text.index("Owned the billing reconciliation service.") < text.index(
        "SKILLS"
    )
    # And no left-column lines appear between right-column lines.
    left = text.index("EXPERIENCE")
    right_start = text.index("SKILLS")
    assert left < right_start
    assert text.index("B.Tech Computer Science") > text.index("Kafka, Redis")


def test_extract_docx_includes_tables_after_paragraphs():
    text = resume_extract.extract_text((FIXTURES / "sample.docx").read_bytes(), ".docx")
    assert text.index("Built distributed ingestion") < text.index("Languages |")
    assert "Datastores | PostgreSQL, Redis" in text


def test_extract_txt_latin1_fallback():
    assert resume_extract.extract_txt("café".encode("latin-1")) == "café"


def test_compute_hash_is_sha256():
    assert resume_extract.compute_hash(b"") == (
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
