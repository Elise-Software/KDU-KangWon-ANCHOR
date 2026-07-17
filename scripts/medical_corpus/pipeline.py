from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import tarfile
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup, Tag


ALLOWED_LICENSES = {"CC0", "CC BY", "U.S. Public Domain", "KOGL Type 0", "KOGL Type 1"}
DISALLOWED_LICENSE_MARKERS = ("NC", "ND", "SA")
CHUNK_REQUIRED_FIELDS = (
    "text", "source_url", "title", "institution", "authors", "pmid", "pmcid",
    "license", "published_at", "modified_at", "collected_at", "retracted", "language",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def stable_id(prefix: str, *parts: str) -> str:
    return f"{prefix}:{sha256_text('|'.join(parts))[:24]}"


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or ""))).strip()


def html_to_text(value: str) -> str:
    soup = BeautifulSoup(value or "", "lxml")
    for node in soup(["script", "style", "noscript"]):
        node.decompose()
    return normalize_space(" ".join(soup.stripped_strings))


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def element_text(node: ET.Element | None) -> str:
    if node is None:
        return ""
    return normalize_space(" ".join(node.itertext()))


def child_elements(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in node.iter() if local_name(child.tag) == name]


def first_text(node: ET.Element, name: str) -> str:
    values = child_elements(node, name)
    return element_text(values[0]) if values else ""


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row}) if rows else ["reason"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as exc:  # pragma: no cover - exercised by strict runtime check
        raise RuntimeError("Parquet output requires pyarrow; install requirements-medical-corpus.txt") from exc
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path, compression="zstd")


def canonical_license(value: str) -> str:
    compact = normalize_space(value).upper()
    compact = re.sub(r"\bCREATIVE COMMONS\b", "", compact)
    compact = re.sub(r"\bLICENSE\b", "", compact)
    compact = normalize_space(compact).replace("CC-BY", "CC BY").replace("CC ZERO", "CC0")
    compact = re.sub(r"^CC\s+CC\b", "CC", compact)
    if re.fullmatch(r"CC\s*0(?:\s+1\.0)?", compact):
        return "CC0"
    if re.fullmatch(r"CC BY(?:\s+(?:2\.0|2\.5|3\.0|4\.0))?", compact):
        return "CC BY"
    return normalize_space(value)


def license_allowed(value: str) -> bool:
    canonical = canonical_license(value)
    if canonical not in ALLOWED_LICENSES:
        return False
    if canonical.startswith("CC") and any(marker in canonical.split() for marker in DISALLOWED_LICENSE_MARKERS):
        return False
    return True


@dataclass
class FetchResult:
    url: str
    body: bytes
    content_type: str
    fetched_at: str
    status_code: int


class Collector:
    def __init__(self, config: dict[str, Any], output_dir: Path):
        self.config = config
        self.output_dir = output_dir
        self.raw_dir = output_dir / "raw"
        self.documents: list[dict[str, Any]] = []
        self.exclusions: list[dict[str, Any]] = []
        self.raw_manifest: list[dict[str, Any]] = []
        self.session = requests.Session()
        self.session.headers["User-Agent"] = config["user_agent"]
        self.timeout = int(config.get("request_timeout_seconds", 60))
        self.interval = float(config.get("request_interval_seconds", 0.35))

    def exclude(self, source: str, url: str, reason: str, detail: str = "") -> None:
        self.exclusions.append({
            "source": source,
            "source_url": url,
            "reason": reason,
            "detail": normalize_space(detail),
            "recorded_at": utc_now(),
        })

    def fetch(self, source: str, url: str, suffix: str | None = None) -> FetchResult:
        response = self.session.get(url, timeout=self.timeout)
        response.raise_for_status()
        body = response.content
        fetched_at = utc_now()
        content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
        guessed = suffix or Path(urllib.parse.urlparse(response.url).path).suffix or ".bin"
        if len(guessed) > 8 or not guessed.startswith("."):
            guessed = ".bin"
        filename = f"{source}_{sha256_text(url)[:20]}{guessed}"
        raw_path = self.raw_dir / source / filename
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(body)
        self.raw_manifest.append({
            "source": source,
            "requested_url": url,
            "resolved_url": response.url,
            "raw_path": raw_path.relative_to(self.output_dir).as_posix(),
            "sha256": sha256_bytes(body),
            "bytes": len(body),
            "content_type": content_type,
            "status_code": response.status_code,
            "fetched_at": fetched_at,
        })
        if self.interval:
            time.sleep(self.interval)
        return FetchResult(response.url, body, content_type, fetched_at, response.status_code)

    def add_document(self, row: dict[str, Any]) -> None:
        row = dict(row)
        row["license"] = canonical_license(row.get("license", ""))
        row["retracted"] = bool(row.get("retracted", False))
        row["authors"] = normalize_space(row.get("authors")) or "not_provided"
        row["pmid"] = normalize_space(row.get("pmid")) or "not_applicable"
        row["pmcid"] = normalize_space(row.get("pmcid")) or "not_applicable"
        row["published_at"] = normalize_space(row.get("published_at")) or "not_provided"
        row["modified_at"] = normalize_space(row.get("modified_at")) or "not_provided"
        row["language"] = normalize_space(row.get("language")) or "und"
        row["collected_at"] = normalize_space(row.get("collected_at")) or utc_now()
        sections = [
            {"section_title": normalize_space(item.get("section_title")) or "Body", "text": normalize_space(item.get("text"))}
            for item in row.get("sections", [])
            if normalize_space(item.get("text"))
        ]
        row["sections"] = sections
        full_text = "\n\n".join(item["text"] for item in sections)
        row["text_sha256"] = sha256_text(full_text)
        row["document_id"] = stable_id("meddoc", row.get("source_url", ""), row["text_sha256"])
        if not license_allowed(row["license"]):
            self.exclude(row.get("source", "unknown"), row.get("source_url", ""), "license_not_allowed", row["license"])
            return
        if row["retracted"]:
            self.exclude(row.get("source", "unknown"), row.get("source_url", ""), "retracted", row.get("title", ""))
            return
        if not sections:
            self.exclude(row.get("source", "unknown"), row.get("source_url", ""), "no_usable_text")
            return
        self.documents.append(row)

    def collect_pmc(self, pmcids: list[str]) -> None:
        source = "pmc_oa_comm"
        for raw_pmcid in pmcids:
            pmcid = raw_pmcid.upper().strip()
            api_url = f"https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmcid}"
            try:
                api = self.fetch(source, api_url, ".xml")
                root = ET.fromstring(api.body)
                record = next((node for node in root.iter() if local_name(node.tag) == "record"), None)
                if record is None:
                    self.exclude(source, api_url, "not_in_pmc_oa_subset", element_text(root))
                    continue
                license_value = canonical_license(record.attrib.get("license", ""))
                if license_value not in {"CC0", "CC BY"}:
                    self.exclude(source, api_url, "license_not_allowed", license_value or "missing")
                    continue
                metadata_retracted = normalize_space(record.attrib.get("retracted", "")).casefold()
                if metadata_retracted not in {"no", "false", "0"}:
                    self.exclude(source, api_url, "retracted_or_status_unknown", metadata_retracted or "missing")
                    continue
                link = next((node for node in record.iter() if local_name(node.tag) == "link" and node.attrib.get("format") == "tgz"), None)
                if link is None or not link.attrib.get("href"):
                    self.exclude(source, api_url, "oa_package_missing")
                    continue
                package_url = link.attrib["href"].replace("ftp://ftp.ncbi.nlm.nih.gov", "https://ftp.ncbi.nlm.nih.gov")
                package_urls = [package_url]
                if "/pub/pmc/oa_package/" in package_url:
                    package_urls.append(package_url.replace("/pub/pmc/oa_package/", "/pub/pmc/deprecated/oa_package/"))
                package = None
                last_error: Exception | None = None
                for candidate_url in package_urls:
                    try:
                        package = self.fetch(source, candidate_url, ".tar.gz")
                        package_url = candidate_url
                        break
                    except requests.HTTPError as exc:
                        last_error = exc
                        if exc.response is None or exc.response.status_code != 404:
                            raise
                if package is None:
                    raise last_error or RuntimeError("PMC OA package unavailable")
                with tarfile.open(fileobj=io.BytesIO(package.body), mode="r:gz") as archive:
                    members = [
                        member for member in archive.getmembers()
                        if member.isfile() and Path(member.name).suffix.lower() in {".nxml", ".xml"}
                        and ".." not in Path(member.name).parts
                    ]
                    if not members:
                        raise ValueError("OA package contains no article XML")
                    article_xml = archive.extractfile(members[0]).read()  # type: ignore[union-attr]
                article_url = f"https://pmc.ncbi.nlm.nih.gov/articles/{pmcid}/"
                article = parse_pmc_article(article_xml, article_url, license_value, api.fetched_at)
                article["artifact_url"] = package_url
                article["modified_at"] = normalize_space(link.attrib.get("updated")) or "not_provided"
                article["collection_group"] = "oa_comm"
                article["license_evidence"] = {
                    "oa_record_license": record.attrib.get("license", ""),
                    "oa_record_retracted": record.attrib.get("retracted", ""),
                    "oa_record_id": record.attrib.get("id", ""),
                }
                if article["pmcid"].upper() != pmcid:
                    raise ValueError(f"PMCID mismatch: expected {pmcid}, got {article['pmcid']}")
                self.add_document(article)
            except Exception as exc:
                self.exclude(source, api_url, "collection_or_parse_error", repr(exc))

    def collect_health_topics(self, queries: list[str], maximum: int) -> None:
        source = "medlineplus_health_topics"
        for query in queries:
            params = urllib.parse.urlencode({
                "db": "healthTopics", "term": query, "rettype": "topic",
                "retmax": max(1, maximum), "tool": "kdu-commercial-medical-corpus",
            })
            url = f"https://wsearch.nlm.nih.gov/ws/query?{params}"
            try:
                fetched = self.fetch(source, url, ".xml")
                for row in parse_medlineplus_health_topics(fetched.body, fetched.fetched_at)[:maximum]:
                    self.add_document(row)
            except Exception as exc:
                self.exclude(source, url, "collection_or_parse_error", repr(exc))

    def collect_medical_tests(self, urls: list[str]) -> None:
        source = "medlineplus_medical_tests"
        for url in urls:
            try:
                fetched = self.fetch(source, url, ".html")
                self.add_document(parse_medlineplus_medical_test(fetched.body, fetched.url, fetched.fetched_at))
            except Exception as exc:
                self.exclude(source, url, "collection_or_parse_error", repr(exc))

    def collect_genetics(self, urls: list[str]) -> None:
        source = "medlineplus_genetics"
        for url in urls:
            try:
                fetched = self.fetch(source, url, ".json")
                self.add_document(parse_medlineplus_genetics(fetched.body, fetched.url, fetched.fetched_at))
            except Exception as exc:
                self.exclude(source, url, "collection_or_parse_error", repr(exc))

    def collect_korean_public(self, urls: list[str]) -> None:
        source = "korean_public_documents"
        for url in urls:
            try:
                fetched = self.fetch(source, url, ".html")
                parsed = parse_korean_public_document(fetched.body, fetched.url, fetched.fetched_at)
                if not parsed.get("license"):
                    self.exclude(source, url, "explicit_document_license_missing", "No KOGL Type 0/1 marker on document page")
                    continue
                self.add_document(parsed)
            except Exception as exc:
                self.exclude(source, url, "collection_or_parse_error", repr(exc))


def parse_pmc_article(body: bytes, source_url: str, license_value: str, collected_at: str) -> dict[str, Any]:
    root = ET.fromstring(body)
    title = first_text(root, "article-title")
    ids = {node.attrib.get("pub-id-type", ""): element_text(node) for node in child_elements(root, "article-id")}
    authors = []
    for contrib in child_elements(root, "contrib"):
        if contrib.attrib.get("contrib-type") != "author":
            continue
        surname, given = first_text(contrib, "surname"), first_text(contrib, "given-names")
        name = normalize_space(f"{given} {surname}")
        if name:
            authors.append(name)
    pub_date = next(iter(child_elements(root, "pub-date")), None)
    published = ""
    if pub_date is not None:
        parts = {local_name(node.tag): element_text(node) for node in pub_date}
        published = "-".join(filter(None, (parts.get("year"), parts.get("month", "").zfill(2), parts.get("day", "").zfill(2))))
    sections: list[dict[str, str]] = []
    for abstract in child_elements(root, "abstract")[:1]:
        text = element_text(abstract)
        if text:
            sections.append({"section_title": "Abstract", "text": text})
    bodies = child_elements(root, "body")[:1]
    if bodies:
        body_node = bodies[0]
        direct_sections = [node for node in list(body_node) if local_name(node.tag) == "sec"]
        if direct_sections:
            for section in direct_sections:
                heading = first_text(section, "title") or "Body"
                paragraphs = [element_text(node) for node in section.iter() if local_name(node.tag) in {"p", "list-item"}]
                text = normalize_space(" ".join(dict.fromkeys(filter(None, paragraphs))))
                if text:
                    sections.append({"section_title": heading, "text": text})
        else:
            text = element_text(body_node)
            if text:
                sections.append({"section_title": "Body", "text": text})
    article_type = root.attrib.get("article-type", "").casefold()
    retraction_text = " ".join(
        element_text(node) for node in root.iter()
        if local_name(node.tag) in {"related-article", "subject", "article-title"}
    ).casefold()
    retracted = article_type in {"retraction", "retracted-article"} or "retracted publication" in retraction_text
    pmcid = ids.get("pmc", "") or ids.get("pmcid", "")
    if pmcid and not pmcid.upper().startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    return {
        "source": "pmc_oa_comm", "source_url": source_url, "title": title,
        "institution": "PubMed Central, U.S. National Library of Medicine",
        "authors": "; ".join(authors) or "not_provided", "pmid": ids.get("pmid", ""),
        "pmcid": pmcid, "license": license_value,
        "license_url": "https://pmc.ncbi.nlm.nih.gov/tools/openftlist/",
        "published_at": published, "modified_at": "not_provided", "collected_at": collected_at,
        "retracted": retracted, "language": root.attrib.get("{http://www.w3.org/XML/1998/namespace}lang", "en"),
        "abstract": sections[0]["text"] if sections and sections[0]["section_title"] == "Abstract" else "",
        "sections": sections,
    }


def parse_medlineplus_health_topics(body: bytes, collected_at: str) -> list[dict[str, Any]]:
    root = ET.fromstring(body)
    results: list[dict[str, Any]] = []
    for document in child_elements(root, "document"):
        health = next((node for node in document.iter() if local_name(node.tag) == "health-topic"), None)
        if health is None:
            content = next((node for node in document if local_name(node.tag) == "content" and node.attrib.get("name") == "healthTopic"), None)
            if content is not None and element_text(content):
                try:
                    health = ET.fromstring("".join(content.itertext()))
                except ET.ParseError:
                    health = None
        if health is None:
            continue
        summary_node = next((node for node in health if local_name(node.tag) == "full-summary"), None)
        summary = element_text(summary_node)
        if not summary:
            continue
        results.append({
            "source": "medlineplus_health_topics", "source_url": health.attrib.get("url", document.attrib.get("url", "")),
            "title": health.attrib.get("title", ""), "institution": "U.S. National Library of Medicine",
            "authors": "MedlinePlus, U.S. National Library of Medicine", "pmid": "", "pmcid": "",
            "license": "U.S. Public Domain", "license_url": "https://medlineplus.gov/about/using/usingcontent/",
            "published_at": health.attrib.get("date-created", ""), "modified_at": health.attrib.get("date-modified", ""),
            "collected_at": collected_at, "retracted": False,
            "language": "en" if health.attrib.get("language", "English").casefold() == "english" else health.attrib.get("language", "und"),
            "abstract": summary, "sections": [{"section_title": "Summary", "text": summary}],
        })
    return results


def json_ld_dates(soup: BeautifulSoup) -> tuple[str, str]:
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(node.string or "{}")
        except json.JSONDecodeError:
            continue
        rows = value if isinstance(value, list) else [value]
        for row in rows:
            if isinstance(row, dict):
                return normalize_space(row.get("datePublished")), normalize_space(row.get("dateModified"))
    return "", ""


def html_sections(container: Tag) -> list[dict[str, str]]:
    for node in container.select("script, style, nav, footer, form, .share-buttons, .page-actions, .mplus-nav"):
        node.decompose()
    sections: list[dict[str, str]] = []
    current_title = "Overview"
    buffer: list[str] = []
    def flush() -> None:
        text = normalize_space(" ".join(buffer))
        if text:
            sections.append({"section_title": current_title, "text": text})
        buffer.clear()
    for node in container.find_all(["h2", "h3", "p", "li"], recursive=True):
        text = normalize_space(" ".join(node.stripped_strings))
        if not text:
            continue
        if node.name in {"h2", "h3"}:
            flush()
            current_title = text
        elif not node.find_parent("li"):
            buffer.append(text)
    flush()
    return sections


def parse_medlineplus_medical_test(body: bytes, source_url: str, collected_at: str) -> dict[str, Any]:
    soup = BeautifulSoup(body, "lxml")
    canonical = soup.select_one('link[rel="canonical"]')
    final_url = canonical.get("href", source_url) if canonical else source_url
    if urllib.parse.urlparse(final_url).netloc != "medlineplus.gov" or "/lab-tests/" not in urllib.parse.urlparse(final_url).path:
        raise ValueError("not an allowed MedlinePlus Medical Tests page")
    title_node = soup.select_one("main h1, #mplus-content h1, h1")
    container = soup.select_one("main, #mplus-content")
    if title_node is None or container is None:
        raise ValueError("medical test article structure not found")
    published, modified = json_ld_dates(soup)
    sections = html_sections(container)
    title = normalize_space(" ".join(title_node.stripped_strings))
    sections = [row for row in sections if row["text"] != title and len(row["text"]) >= 40]
    return {
        "source": "medlineplus_medical_tests", "source_url": final_url, "title": title,
        "institution": "U.S. National Library of Medicine", "authors": "MedlinePlus, U.S. National Library of Medicine",
        "pmid": "", "pmcid": "", "license": "U.S. Public Domain",
        "license_url": "https://medlineplus.gov/about/using/usingcontent/", "published_at": published,
        "modified_at": modified, "collected_at": collected_at, "retracted": False,
        "language": (soup.html.get("lang", "en") if soup.html else "en"), "abstract": "", "sections": sections,
    }


def parse_medlineplus_genetics(body: bytes, source_url: str, collected_at: str) -> dict[str, Any]:
    data = json.loads(body)
    if urllib.parse.urlparse(source_url).netloc != "medlineplus.gov" or "/download/genetics/" not in urllib.parse.urlparse(source_url).path:
        raise ValueError("not an allowed MedlinePlus Genetics API URL")
    sections = []
    for index, item in enumerate(data.get("text-list", []), start=1):
        text_data = item.get("text", {}) if isinstance(item, dict) else {}
        text = html_to_text(text_data.get("html", ""))
        if text:
            sections.append({"section_title": normalize_space(text_data.get("text-role")) or f"Section {index}", "text": text})
    page_url = data.get("ghr_page") or source_url.replace("/download/", "/").removesuffix(".json")
    return {
        "source": "medlineplus_genetics", "source_url": page_url, "title": normalize_space(data.get("name")),
        "institution": "U.S. National Library of Medicine", "authors": "MedlinePlus Genetics, U.S. National Library of Medicine",
        "pmid": "", "pmcid": "", "license": "U.S. Public Domain",
        "license_url": "https://medlineplus.gov/about/using/usingcontent/", "published_at": data.get("published", ""),
        "modified_at": data.get("reviewed", ""), "collected_at": collected_at, "retracted": False,
        "language": "en", "abstract": sections[0]["text"] if sections else "", "sections": sections,
    }


def detect_kogl_license(soup: BeautifulSoup) -> str:
    evidence = normalize_space(" ".join(soup.stripped_strings))
    attributes = " ".join(
        normalize_space(value)
        for node in soup.find_all(True)
        for key in ("alt", "title", "href", "src")
        if (value := node.get(key))
    )
    joined = f"{evidence} {attributes}".casefold()
    if re.search(r"공공누리\s*(?:제)?\s*0\s*유형|kogl[_/-]?(?:type)?0", joined):
        return "KOGL Type 0"
    if re.search(r"공공누리\s*(?:제)?\s*1\s*유형|kogl[_/-]?(?:type)?1", joined):
        return "KOGL Type 1"
    return ""


def parse_korean_public_document(body: bytes, source_url: str, collected_at: str) -> dict[str, Any]:
    soup = BeautifulSoup(body, "lxml")
    license_value = detect_kogl_license(soup)
    title_node = soup.select_one('meta[property="og:title"]')
    title = normalize_space(title_node.get("content")) if title_node else normalize_space(soup.title.string if soup.title else "")
    container = soup.select_one("main, article, #content, .content") or soup.body
    if container is None:
        raise ValueError("document body not found")
    sections = [row for row in html_sections(container) if len(row["text"]) >= 40]
    institution_node = soup.select_one('meta[property="og:site_name"]')
    author_node = soup.select_one('meta[name="author"]')
    return {
        "source": "korean_public_documents", "source_url": source_url, "title": title,
        "institution": normalize_space(institution_node.get("content")) if institution_node else "not_provided",
        "authors": normalize_space(author_node.get("content")) if author_node else "not_provided",
        "pmid": "", "pmcid": "", "license": license_value,
        "license_url": "https://www.kogl.or.kr/info/userGuide.do", "published_at": "", "modified_at": "",
        "collected_at": collected_at, "retracted": False, "language": "ko", "abstract": "", "sections": sections,
    }


def split_text(text: str, maximum: int, minimum: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?。！？])\s+|\n+", normalize_space(text))
    chunks: list[str] = []
    current = ""
    for sentence in filter(None, map(normalize_space, sentences)):
        if len(sentence) > maximum:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(sentence[index:index + maximum] for index in range(0, len(sentence), maximum))
            continue
        candidate = f"{current} {sentence}".strip()
        if current and len(candidate) > maximum:
            chunks.append(current)
            current = sentence
        else:
            current = candidate
    if current:
        if chunks and len(current) < minimum and len(chunks[-1]) + 1 + len(current) <= maximum:
            chunks[-1] = f"{chunks[-1]} {current}"
        else:
            chunks.append(current)
    return [value for value in chunks if len(value) >= minimum or len(chunks) == 1]


def make_chunks(documents: list[dict[str, Any]], maximum: int, minimum: int) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    for document in documents:
        index = 0
        for section in document["sections"]:
            for text in split_text(section["text"], maximum, minimum):
                text_hash = sha256_text(text.casefold())
                if text_hash in seen_hashes:
                    continue
                seen_hashes.add(text_hash)
                index += 1
                output.append({
                    "chunk_id": stable_id("medchunk", document["document_id"], str(index), text_hash),
                    "document_id": document["document_id"], "chunk_index": index,
                    "source": document["source"],
                    "section_title": section["section_title"], "text": text,
                    "source_url": document["source_url"], "title": document["title"],
                    "institution": document["institution"], "authors": document["authors"],
                    "pmid": document["pmid"], "pmcid": document["pmcid"], "license": document["license"],
                    "license_url": document["license_url"], "published_at": document["published_at"],
                    "modified_at": document["modified_at"], "collected_at": document["collected_at"],
                    "retracted": document["retracted"], "language": document["language"], "text_sha256": text_hash,
                    "artifact_url": document.get("artifact_url", "not_applicable"),
                    "collection_group": document.get("collection_group", "not_applicable"),
                    "license_evidence": (
                        document.get("license_evidence", "")
                        if isinstance(document.get("license_evidence"), str)
                        else json.dumps(document.get("license_evidence", {}), ensure_ascii=False, sort_keys=True)
                    ),
                })
    return output


def run_pipeline(config_path: Path, output_dir: Path, limit_per_source: int = 0, strict: bool = False) -> int:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    collector = Collector(config, output_dir)
    sources = config["sources"]
    limit = lambda values: list(values)[:limit_per_source] if limit_per_source else list(values)
    pmc = sources["pmc_oa_comm"]
    if pmc.get("enabled"):
        collector.collect_pmc(limit(pmc.get("pmcids", [])))
    topics = sources["medlineplus_health_topics"]
    if topics.get("enabled"):
        collector.collect_health_topics(limit(topics.get("queries", [])), int(topics.get("maximum_results_per_query", 1)))
    tests = sources["medlineplus_medical_tests"]
    if tests.get("enabled"):
        collector.collect_medical_tests(limit(tests.get("urls", [])))
    genetics = sources["medlineplus_genetics"]
    if genetics.get("enabled"):
        collector.collect_genetics(limit(genetics.get("urls", [])))
    korean = sources["korean_public_documents"]
    if korean.get("enabled"):
        collector.collect_korean_public(limit(korean.get("urls", [])))

    unique_documents: list[dict[str, Any]] = []
    seen_document_hashes: set[str] = set()
    for row in collector.documents:
        if row["text_sha256"] in seen_document_hashes:
            collector.exclude(row["source"], row["source_url"], "duplicate_document", row["text_sha256"])
            continue
        seen_document_hashes.add(row["text_sha256"])
        unique_documents.append(row)
    chunking = config["chunking"]
    chunks = make_chunks(unique_documents, int(chunking["maximum_characters"]), int(chunking["minimum_characters"]))
    missing = [
        {"chunk_id": row.get("chunk_id", ""), "field": field}
        for row in chunks for field in CHUNK_REQUIRED_FIELDS
        if field not in row or row[field] is None or (isinstance(row[field], str) and not row[field].strip())
    ]
    invalid = [row["chunk_id"] for row in chunks if not license_allowed(row["license"]) or row["retracted"]]

    processed = output_dir / "processed"
    reports = output_dir / "reports"
    document_rows = [{**row, "sections": json.dumps(row["sections"], ensure_ascii=False)} for row in unique_documents]
    write_jsonl(processed / "documents.jsonl", unique_documents)
    write_jsonl(processed / "chunks.jsonl", chunks)
    write_parquet(processed / "documents.parquet", document_rows)
    write_parquet(processed / "chunks.parquet", chunks)
    write_csv(output_dir / "raw" / "manifest.csv", collector.raw_manifest)
    write_jsonl(reports / "exclusions.jsonl", collector.exclusions)
    write_csv(reports / "exclusions.csv", collector.exclusions)
    write_jsonl(reports / "missing_fields.jsonl", missing)
    license_audit = [{
        "decision": "included", "source": row["source"], "source_url": row["source_url"],
        "document_id": row["document_id"], "license": row["license"], "retracted": row["retracted"],
        "license_url": row["license_url"],
        "license_evidence": row.get("license_evidence", {"policy_url": row["license_url"]}),
    } for row in unique_documents]
    license_audit.extend({
        "decision": "excluded", "source": row["source"], "source_url": row["source_url"],
        "document_id": "", "license": "", "retracted": row["reason"].startswith("retracted"),
        "license_url": "", "license_evidence": {"reason": row["reason"], "detail": row["detail"]},
    } for row in collector.exclusions)
    write_jsonl(reports / "license_audit.jsonl", license_audit)
    source_counts: dict[str, int] = {}
    for row in unique_documents:
        source_counts[row["source"]] = source_counts.get(row["source"], 0) + 1
    checks = {
        "documents_present": bool(unique_documents), "chunks_present": bool(chunks),
        "allowed_licenses_only": not invalid, "retracted_documents": sum(bool(row["retracted"]) for row in unique_documents),
        "duplicate_document_count": sum(row["reason"] == "duplicate_document" for row in collector.exclusions),
        "duplicate_chunk_count": len(chunks) - len({row["text_sha256"] for row in chunks}),
        "missing_required_chunk_fields": len(missing),
        "raw_sha256_complete": all(len(row["sha256"]) == 64 for row in collector.raw_manifest),
    }
    passed = all((
        checks["documents_present"], checks["chunks_present"], checks["allowed_licenses_only"],
        checks["retracted_documents"] == 0, checks["duplicate_chunk_count"] == 0,
        checks["missing_required_chunk_fields"] == 0, checks["raw_sha256_complete"],
    ))
    report = {
        "schema_version": config["schema_version"], "generated_at": utc_now(),
        "config_path": f"config/{config_path.name}", "output_dir": "data/commercial_medical_corpus",
        "document_count": len(unique_documents), "chunk_count": len(chunks),
        "raw_record_count": len(collector.raw_manifest), "exclusion_count": len(collector.exclusions),
        "source_document_counts": source_counts, "licenses": sorted({row["license"] for row in unique_documents}),
        "checks": checks, "integrity_checks_passed": passed,
        "dataset_status": "verified" if passed and not collector.exclusions else "conditionally_verified",
        "outputs": {
            "documents_jsonl": "processed/documents.jsonl", "documents_parquet": "processed/documents.parquet",
            "chunks_jsonl": "processed/chunks.jsonl", "chunks_parquet": "processed/chunks.parquet",
            "raw_manifest": "raw/manifest.csv", "exclusions": "reports/exclusions.jsonl",
            "license_audit": "reports/license_audit.jsonl",
        },
    }
    write_json(reports / "pipeline_report.json", report)
    if strict and not passed:
        raise RuntimeError(f"commercial medical corpus integrity checks failed: {checks}")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0
