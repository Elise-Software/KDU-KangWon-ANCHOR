from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from medical_corpus.pipeline import (  # noqa: E402
    CHUNK_REQUIRED_FIELDS,
    canonical_license,
    detect_kogl_license,
    license_allowed,
    make_chunks,
    parse_medlineplus_genetics,
    parse_medlineplus_health_topics,
    parse_medlineplus_medical_test,
    parse_pmc_article,
)
from medical_corpus.bulk_pipeline import (  # noqa: E402
    _selection_files_sha256,
    _write_jsonl_zstd_shards,
    classify_domain,
    limit_chunks_per_document,
    pmcid_from_archive_member,
)
from bs4 import BeautifulSoup  # noqa: E402


def test_license_gate_only_allows_requested_commercial_terms() -> None:
    assert canonical_license("Creative Commons CC-BY 4.0 license") == "CC BY"
    for value in ("CC0", "CC BY", "U.S. Public Domain", "KOGL Type 0", "KOGL Type 1"):
        assert license_allowed(value)
    for value in ("CC BY-NC", "CC BY-ND", "CC BY-SA", "NO-CC CODE", "All rights reserved", ""):
        assert not license_allowed(value)


def test_current_pmc_transition_path_can_be_derived_from_legacy_oa_path() -> None:
    legacy = "https://ftp.ncbi.nlm.nih.gov/pub/pmc/oa_package/38/22/PMC1043859.tar.gz"
    assert legacy.replace("/pub/pmc/oa_package/", "/pub/pmc/deprecated/oa_package/") == (
        "https://ftp.ncbi.nlm.nih.gov/pub/pmc/deprecated/oa_package/38/22/PMC1043859.tar.gz"
    )


def test_pmc_parser_preserves_article_metadata_and_sections() -> None:
    xml = b"""<article article-type="research-article" xml:lang="en">
      <front><article-meta><article-id pub-id-type="pmc">PMC123</article-id>
      <article-id pub-id-type="pmid">456</article-id><title-group><article-title>Safe sample</article-title></title-group>
      <contrib-group><contrib contrib-type="author"><name><surname>Kim</surname><given-names>A</given-names></name></contrib></contrib-group>
      <pub-date><year>2025</year><month>2</month><day>3</day></pub-date>
      <abstract><p>Abstract medical evidence with enough content for retrieval.</p></abstract></article-meta></front>
      <body><sec><title>Methods</title><p>Section body with methods and reproducible details.</p></sec></body></article>"""
    row = parse_pmc_article(xml, "https://ftp.ncbi.nlm.nih.gov/sample.tgz", "CC BY", "2026-07-17T00:00:00+00:00")
    assert row["pmcid"] == "PMC123"
    assert row["pmid"] == "456"
    assert row["authors"] == "A Kim"
    assert row["published_at"] == "2025-02-03"
    assert [section["section_title"] for section in row["sections"]] == ["Abstract", "Methods"]
    assert not row["retracted"]


def test_pmc_parser_flags_retraction() -> None:
    xml = b"""<article article-type="retraction"><front><article-meta>
      <article-id pub-id-type="pmc">PMC999</article-id><title-group><article-title>Retraction</article-title></title-group>
      </article-meta></front><body><p>This article is retracted.</p></body></article>"""
    assert parse_pmc_article(xml, "https://example", "CC BY", "now")["retracted"]


def test_health_topic_parser_uses_public_domain_summary_only() -> None:
    xml = b"""<nlmSearchResult><list><document url="https://medlineplus.gov/headache.html">
      <content name="healthTopic"><health-topic title="Headache" url="https://medlineplus.gov/headache.html"
      language="English" date-created="01/01/2020"><full-summary><p>Headache summary text.</p></full-summary>
      <site><title>External copyrighted link is metadata, not corpus text</title></site></health-topic></content>
      </document></list></nlmSearchResult>"""
    rows = parse_medlineplus_health_topics(xml, "2026-07-17T00:00:00+00:00")
    assert len(rows) == 1
    assert rows[0]["license"] == "U.S. Public Domain"
    assert rows[0]["sections"][0]["text"] == "Headache summary text."
    assert "copyrighted" not in rows[0]["sections"][0]["text"]


def test_medical_test_parser_removes_navigation_and_keeps_article_sections() -> None:
    html = b"""<html lang="en"><head><title>CBC</title>
      <link rel="canonical" href="https://medlineplus.gov/lab-tests/complete-blood-count-cbc/" />
      <script type="application/ld+json">{"datePublished":"2024-01-01","dateModified":"2025-02-02"}</script></head>
      <body><nav>Copyrighted drug link</nav><main><h1>Complete Blood Count</h1>
      <h2>What is it?</h2><p>A complete blood count measures cells in your blood and helps a clinician assess health.</p>
      <h2>What happens?</h2><p>A health professional takes a blood sample for testing in a laboratory.</p></main></body></html>"""
    row = parse_medlineplus_medical_test(html, "https://medlineplus.gov/lab-tests/complete-blood-count-cbc/", "now")
    assert row["title"] == "Complete Blood Count"
    assert row["published_at"] == "2024-01-01"
    assert all("Copyrighted drug" not in section["text"] for section in row["sections"])


def test_genetics_parser_uses_api_text_list() -> None:
    payload = {
        "name": "Example condition", "ghr_page": "https://medlineplus.gov/genetics/condition/example",
        "text-list": [{"text": {"text-role": "description", "html": "<p>Consumer genetics summary.</p>"}}],
        "published": "2024-03-01", "reviewed": "2024-02",
    }
    row = parse_medlineplus_genetics(
        json.dumps(payload).encode(), "https://medlineplus.gov/download/genetics/condition/example.json", "now"
    )
    assert row["source_url"].endswith("/genetics/condition/example")
    assert row["sections"] == [{"section_title": "description", "text": "Consumer genetics summary."}]


@pytest.mark.parametrize(
    ("marker", "expected"),
    (("공공누리 제0유형", "KOGL Type 0"), ("공공누리 제1유형", "KOGL Type 1"), ("공공누리 제2유형", "")),
)
def test_kogl_license_must_be_explicit_on_document(marker: str, expected: str) -> None:
    soup = BeautifulSoup(f"<html><body><img alt='{marker}'></body></html>", "lxml")
    assert detect_kogl_license(soup) == expected


def test_chunks_include_provenance_and_are_deduplicated() -> None:
    document = {
        "document_id": "meddoc:1", "source": "pmc_oa_comm", "source_url": "https://example.test/doc", "title": "Title",
        "institution": "Institution", "authors": "Author", "pmid": "not_applicable", "pmcid": "not_applicable",
        "license": "CC BY", "license_url": "https://license", "published_at": "2025-01-01",
        "modified_at": "2025-02-01", "collected_at": "2026-07-17", "retracted": False,
        "language": "en", "sections": [
            {"section_title": "A", "text": "This is a sufficiently long evidence sentence for a medical retrieval corpus."},
            {"section_title": "B", "text": "This is a sufficiently long evidence sentence for a medical retrieval corpus."},
        ],
    }
    rows = make_chunks([document], maximum=200, minimum=20)
    assert len(rows) == 1
    assert not [field for field in CHUNK_REQUIRED_FIELDS if field not in rows[0]]
    assert rows[0]["license"] == "CC BY"


def test_bulk_domain_classification_is_deterministic_and_not_oncology_only() -> None:
    balance = {
        "domains": {
            "cardiovascular": ["heart", "hypertension"],
            "oncology": ["cancer", "tumor"],
            "general_other": [],
        }
    }
    assert classify_domain("Hypertension and heart disease treatment", balance) == "cardiovascular"
    assert classify_domain("Cancer tumor biomarkers", balance) == "oncology"
    assert classify_domain("Methods for clinical research", balance) == "general_other"


def test_bulk_domain_classification_does_not_match_short_substrings() -> None:
    balance = {
        "domains": {
            "ophthalmology_ent": ["eye", "ear", "hearing"],
            "cardiovascular": ["heart", "cardiac"],
            "general_other": [],
        }
    }
    assert classify_domain("Research methods and cardiac outcomes", balance) == "cardiovascular"
    assert classify_domain("A systematic research methods review", balance) == "general_other"
    assert classify_domain("Hearing loss and ear disease", balance) == "ophthalmology_ent"


def test_long_documents_are_sampled_evenly_instead_of_truncated() -> None:
    chunks = [
        {"document_id": "doc", "chunk_index": index, "chunk_id": f"old-{index}", "text_sha256": f"hash-{index}"}
        for index in range(100)
    ]
    selected = limit_chunks_per_document(chunks, 5)
    assert [row["text_sha256"] for row in selected] == ["hash-0", "hash-25", "hash-50", "hash-74", "hash-99"]
    assert [row["chunk_index"] for row in selected] == [1, 2, 3, 4, 5]
    assert len({row["chunk_id"] for row in selected}) == 5


def test_missing_xml_pmcid_can_only_be_recovered_from_exact_member_name() -> None:
    assert pmcid_from_archive_member("PMC005xxxxxx/PMC5023792.xml") == "PMC5023792"
    assert pmcid_from_archive_member("nested/pmc12345.nxml") == "PMC12345"
    assert pmcid_from_archive_member("nested/article-12345.xml") == ""
    assert pmcid_from_archive_member("nested/PMC12345.xml.backup") == ""


def test_selection_fingerprint_covers_every_partition_fragment(tmp_path: Path) -> None:
    first = tmp_path / "first.parquet"
    second = tmp_path / "second.parquet"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    original = _selection_files_sha256([second, first])
    assert original == _selection_files_sha256([first, second])
    second.write_bytes(b"changed")
    assert original != _selection_files_sha256([first, second])


def test_native_jsonl_export_preserves_every_parquet_row(tmp_path: Path) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    zstandard = pytest.importorskip("zstandard")
    parquet = tmp_path / "sample.parquet"
    pq.write_table(pa.Table.from_pylist([
        {"document_id": "doc:1", "text": "첫 번째"},
        {"document_id": "doc:2", "text": "second"},
    ]), parquet)
    destination = tmp_path / "jsonl"
    report = _write_jsonl_zstd_shards(parquet, destination, rows_per_shard=1)
    with (destination / "part-00000.jsonl.zst").open("rb") as handle:
        with zstandard.ZstdDecompressor().stream_reader(handle) as reader:
            decoded = io.BufferedReader(reader).read().decode("utf-8")
    rows = [json.loads(line) for line in decoded.splitlines()]
    assert report == {"row_count": 2, "shard_count": 1}
    assert [row["document_id"] for row in rows] == ["doc:1", "doc:2"]
