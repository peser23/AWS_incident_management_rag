#!/usr/bin/env python3
"""DAIMO Chunk+Embed job (EC2, cron-driven).

Reads redacted ServiceNow incident records (JSON array, single JSON object, or
JSONL) from the S3 clean bucket (daimo-s3-clean), builds three chunk types per incident
(symptom / resolution / full_narrative), embeds each chunk with Amazon Titan
Text Embeddings V2 on Bedrock, and upserts them into a local FAISS index.

POC stand-ins (see DAIMO_Implementation_Context.md):
  * Local FAISS index instead of the OpenSearch domain (not yet provisioned).
  * Failures go to a local JSONL file, and to DynamoDB when
    DAIMO_FAILURES_TABLE is set.

Guarantees:
  * Idempotent: chunk ids are `<incident_number>#<chunk_type>[#<window>]`.
    Re-runs replace an incident's chunks and never duplicate them; unchanged
    incidents (same content hash) are skipped without calling Bedrock, and S3
    objects whose ETag is unchanged since the last fully successful run are not
    re-downloaded.
  * No silent failures: every per-record error is written to the failures sink
    with a stage and reason, and the process exits non-zero.
  * Redaction before embedding: the clean bucket holds redacted records. A
    residual-PII scan backs this up; DAIMO_PII_GUARD=warn (default) logs any
    structured PII/PHI it still finds and indexes the record, =block rejects
    the record to the failures sink, =off skips the scan.
  * Crash-safe: the index and manifest are written to a temp directory and
    swapped in atomically; a lock file prevents overlapping cron runs.

Usage:
  chunk_embed.py                          # process s3://$DAIMO_CLEAN_BUCKET/$DAIMO_CLEAN_PREFIX
  chunk_embed.py --input path/to/file.json --input another_dir/   # local files instead of S3
  chunk_embed.py --dry-run                # build + guard chunks, no Bedrock/index writes
  chunk_embed.py --force                  # re-embed even unchanged incidents
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import boto3
import faiss
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from langchain_aws import BedrockEmbeddings
from langchain_community.docstore.in_memory import InMemoryDocstore
from langchain_community.vectorstores import FAISS
from langchain_community.vectorstores.utils import DistanceStrategy
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

STAGE = "chunk_embed"
LOG = logging.getLogger("daimo.chunk_embed")

# Bedrock errors that will not fix themselves by moving on to the next record.
FATAL_BEDROCK_ERRORS = {
    "AccessDeniedException",
    "UnrecognizedClientException",
    "ExpiredTokenException",
    "ResourceNotFoundException",
}


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    aws_region: str = _env("AWS_REGION", "us-east-1")
    embed_model_id: str = _env("DAIMO_EMBED_MODEL_ID", "amazon.titan-embed-text-v2:0")
    embed_dimensions: int = int(_env("DAIMO_EMBED_DIMENSIONS", "1024"))
    bedrock_max_attempts: int = int(_env("DAIMO_BEDROCK_MAX_ATTEMPTS", "8"))
    clean_bucket: str = _env("DAIMO_CLEAN_BUCKET", "daimo-s3-clean")
    clean_prefix: str = _env("DAIMO_CLEAN_PREFIX", "")
    pii_guard: str = _env("DAIMO_PII_GUARD", "warn").lower()  # warn | block | off
    index_dir: Path = Path(_env("DAIMO_INDEX_DIR", "/home/ec2-user/daimo/data/index"))
    index_name: str = _env("DAIMO_INDEX_NAME", "incidents")
    failures_file: Path = Path(_env("DAIMO_FAILURES_FILE", "/home/ec2-user/daimo/logs/embed_failures.jsonl"))
    failures_table: str = _env("DAIMO_FAILURES_TABLE", "")
    initial_notes: int = int(_env("DAIMO_SYMPTOM_INITIAL_NOTES", "2"))
    # Titan V2 accepts up to 8,192 tokens / 50,000 chars; windows stay well under.
    narrative_max_chars: int = int(_env("DAIMO_NARRATIVE_MAX_CHARS", "6000"))
    narrative_overlap_chars: int = int(_env("DAIMO_NARRATIVE_OVERLAP_CHARS", "600"))
    checkpoint_every: int = int(_env("DAIMO_CHECKPOINT_EVERY", "50"))


# --------------------------------------------------------------------------- #
# Failure sink (local JSONL + optional DynamoDB)
# --------------------------------------------------------------------------- #
class FailureSink:
    def __init__(self, settings: Settings, run_id: str):
        self.run_id = run_id
        self.path = settings.failures_file
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.table = None
        if settings.failures_table:
            self.table = boto3.resource("dynamodb", region_name=settings.aws_region).Table(
                settings.failures_table
            )
        self.count = 0

    def record(self, incident_id: str | None, reason: str, error: Exception | None = None,
               source: str | None = None) -> None:
        self.count += 1
        item = {
            "failure_id": f"{self.run_id}#{self.count}",
            "run_id": self.run_id,
            "stage": STAGE,
            "incident_id": incident_id or "UNKNOWN",
            "reason": reason,
            "error_type": type(error).__name__ if error else None,
            "error_detail": str(error)[:1000] if error else None,
            "source": source,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        LOG.error("failure incident=%s reason=%s error=%s", item["incident_id"], reason,
                  item["error_detail"])
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item) + "\n")
        if self.table is not None:
            try:
                self.table.put_item(Item={k: v for k, v in item.items() if v is not None})
            except (ClientError, BotoCoreError) as exc:  # local JSONL already has it
                LOG.error("could not write failure to DynamoDB table: %s", exc)


# --------------------------------------------------------------------------- #
# Residual PII/PHI guard (backstop for the Redaction stage)
# --------------------------------------------------------------------------- #
_STREET_SUFFIX = (r"(?:St|Street|Ave|Avenue|Rd|Road|Blvd|Boulevard|Dr|Drive|Ln|Lane|Ter|"
                  r"Terrace|Ct|Court|Way|Pl|Place|Cir|Circle|Pkwy|Parkway|Hwy|Highway)")
PII_PATTERNS: dict[str, re.Pattern[str]] = {
    "US_SSN": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "PHONE_NUMBER": re.compile(r"(?<![\w-])\(?\d{3}\)?[-. ]\d{3,4}[-. ]\d{4}\b"),
    "DATE_OF_BIRTH": re.compile(
        r"(?i)\b(?:dob|d\.o\.b\.?|date of birth|birth ?date)\b\W{0,3}"
        r"\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}"),
    "EMAIL_ADDRESS": re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"),
    "MEMBER_ICN": re.compile(r"\b\d{10}V\d{6}\b"),
    "STREET_ADDRESS": re.compile(rf"\b\d{{1,6}}\s+(?:[A-Z][a-z]+\s+){{1,3}}{_STREET_SUFFIX}\b\.?"),
}
_CARD_CANDIDATE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def find_residual_pii(text: str) -> list[str]:
    """Return the PII *types* found (never the values, so they can be logged)."""
    found = [name for name, rx in PII_PATTERNS.items() if rx.search(text)]
    for match in _CARD_CANDIDATE.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn_ok(digits):
            found.append("CREDIT_CARD")
            break
    return found


# --------------------------------------------------------------------------- #
# Chunking
# --------------------------------------------------------------------------- #
_BOILERPLATE_PREFIX = re.compile(r"^\s*(?:resolution note|partner update)\s*[-:]\s*", re.I)
_LEVEL = re.compile(r"^\s*(\d)")


_NULLISH = {"none", "n/a", "na", "null", "-"}


def _clean(text: Any) -> str:
    if not text or str(text).strip().lower() in _NULLISH:
        return ""
    text = _BOILERPLATE_PREFIX.sub("", str(text))
    return re.sub(r"\s+", " ", text).strip()


def _level(value: Any) -> int | None:
    m = _LEVEL.match(str(value or ""))
    return int(m.group(1)) if m else None


def _sorted_notes(record: dict) -> list[dict]:
    notes = [n for n in record.get("work_notes") or [] if _clean(n.get("note"))]
    return sorted(notes, key=lambda n: n.get("timestamp") or "")


def _base_metadata(record: dict, content_hash: str, source: str, settings: Settings) -> dict:
    closure = record.get("closure_information") or {}
    notes = _sorted_notes(record)
    return {
        "incident_number": record["incident_number"],
        "short_description": _clean(record.get("short_description")),
        "state": record.get("state"),
        "priority": record.get("priority"),
        "priority_level": _level(record.get("priority")),
        "impact_level": _level(record.get("impact")),
        "urgency_level": _level(record.get("urgency")),
        "category": record.get("category") or None,
        "subcategory": record.get("subcategory") or None,
        "assignment_group": record.get("assignment_group") or None,
        "affected_service": record.get("affected_service") or None,
        "affected_system_name": record.get("affected_system_name") or None,
        "affected_ci": record.get("affected_ci") or None,
        "location_service_area": record.get("location_service_area") or None,
        "initiative": record.get("initiative") or None,
        "configuration_items": [c.get("configuration_item") for c in record.get("impacted_cis") or []
                                if c.get("configuration_item")],
        "related_kb_articles": list(record.get("related_kb_articles") or []),
        "resolution_code": closure.get("resolution_code"),
        "technical_cause": closure.get("technical_cause"),
        "caused_by_change": closure.get("caused_by_change"),
        "first_note_at": notes[0].get("timestamp") if notes else None,
        "closed_at": closure.get("closed"),
        "content_hash": content_hash,
        "source_file": source,
        "embedding_model": settings.embed_model_id,
        "embedding_dimensions": settings.embed_dimensions,
    }


def build_chunks(record: dict, content_hash: str, source: str, settings: Settings,
                 splitter: RecursiveCharacterTextSplitter) -> list[Document]:
    """Build symptom / resolution / full_narrative chunks for one incident.

    Only semantic fields are embedded; people, phone numbers, external owners and
    `evaluation_metadata` are deliberately left out of both text and metadata.
    """
    inc = record["incident_number"]
    short = _clean(record.get("short_description"))
    desc = _clean(record.get("detailed_description"))
    service = _clean(record.get("affected_service") or record.get("affected_system_name")
                     or record.get("affected_ci"))
    closure = record.get("closure_information") or {}
    notes = _sorted_notes(record)
    meta = _base_metadata(record, content_hash, source, settings)
    header = f"Incident {inc}: {short}" if short else f"Incident {inc}"

    docs: list[Document] = []

    def add(chunk_type: str, text: str, window: int | None = None) -> None:
        doc_id = f"{inc}#{chunk_type}" + (f"#{window}" if window is not None else "")
        docs.append(Document(id=doc_id, page_content=text,
                             metadata={**meta, "chunk_id": doc_id, "chunk_type": chunk_type,
                                       "window": window}))

    # symptom: what the operator would have seen at intake.
    parts = [header]
    if service:
        parts.append(f"Affected service: {service}")
    if desc:
        parts.append(f"Description: {desc}")
    initial = [_clean(n["note"]) for n in notes[: settings.initial_notes]]
    if initial:
        parts.append("Initial notes: " + " | ".join(initial))
    if len(parts) > 1:
        add("symptom", "\n".join(parts))

    # resolution: how it was closed.
    res_notes = _clean(closure.get("resolution_notes"))
    parts = [header]
    for label, key in (("Resolution code", "resolution_code"),
                       ("Technical cause", "technical_cause"),
                       ("Caused by change", "caused_by_change")):
        if closure.get(key):
            parts.append(f"{label}: {_clean(closure[key])}")
    if res_notes:
        parts.append(f"Resolution notes: {res_notes}")
    if notes:
        parts.append(f"Final work note: {_clean(notes[-1]['note'])}")
    if res_notes or closure.get("resolution_code"):
        add("resolution", "\n".join(parts))

    # full_narrative: chronological story, windowed if long.
    body = "\n".join(filter(None, [
        f"Description: {desc}" if desc else "",
        *(f"[{n.get('timestamp', '')}] {_clean(n['note'])}" for n in notes),
        f"Resolution: {res_notes}" if res_notes else "",
    ]))
    if body:
        windows = splitter.split_text(body)
        for i, window in enumerate(windows):
            add("full_narrative", f"{header}\n{window}", i if len(windows) > 1 else None)

    return docs


def content_hash_of(record: dict) -> str:
    """Hash of the full record minus eval ground truth, so any upstream change re-embeds."""
    payload = {k: v for k, v in record.items() if k != "evaluation_metadata"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


# --------------------------------------------------------------------------- #
# Input loading
# --------------------------------------------------------------------------- #
@dataclass
class Source:
    name: str                         # s3://bucket/key or local path
    version: str | None               # S3 ETag; None for local files
    load: Callable[[], list[dict]]


_INPUT_SUFFIXES = (".json", ".jsonl")


def parse_records(name: str, text: str) -> list[dict]:
    if name.endswith(".jsonl"):
        records = []
        for line_no, line in enumerate(text.splitlines(), 1):
            if line.strip():
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{name}:{line_no}: invalid JSON line") from exc
        return records
    data = json.loads(text)
    return data if isinstance(data, list) else [data]


def iter_s3_sources(s3: Any, bucket: str, prefix: str) -> Iterator[Source]:
    def loader(key: str) -> Callable[[], list[dict]]:
        return lambda: parse_records(
            key, s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8"))

    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith(_INPUT_SUFFIXES):
                yield Source(f"s3://{bucket}/{obj['Key']}", obj["ETag"], loader(obj["Key"]))


def iter_local_sources(paths: list[Path]) -> Iterator[Source]:
    for p in paths:
        if p.is_dir():
            files = sorted(f for f in p.rglob("*") if f.name.endswith(_INPUT_SUFFIXES) and f.is_file())
        elif p.is_file():
            files = [p]
        else:
            raise FileNotFoundError(f"input path not found: {p}")
        for f in files:
            yield Source(str(f), None, lambda f=f: parse_records(f.name, f.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------- #
# FAISS store + manifest
# --------------------------------------------------------------------------- #
class IncidentIndex:
    """FAISS store plus a manifest of incident -> (content hash, chunk ids)."""

    MANIFEST = "manifest.json"

    def __init__(self, settings: Settings, embeddings: BedrockEmbeddings):
        self.s = settings
        self.embeddings = embeddings
        self.dir = settings.index_dir
        if (self.dir / f"{settings.index_name}.faiss").exists():
            self.store = FAISS.load_local(str(self.dir), embeddings, index_name=settings.index_name,
                                          allow_dangerous_deserialization=True)  # our own pickle
            self.manifest = json.loads((self.dir / self.MANIFEST).read_text())
            self.manifest.setdefault("s3_objects", {})
            if (self.manifest.get("embedding_model") != settings.embed_model_id
                    or self.manifest.get("embedding_dimensions") != settings.embed_dimensions
                    or self.store.index.d != settings.embed_dimensions):
                raise RuntimeError(
                    f"index at {self.dir} was built with {self.manifest.get('embedding_model')} "
                    f"dim={self.store.index.d}; config is {settings.embed_model_id} "
                    f"dim={settings.embed_dimensions}. Rebuild into a new DAIMO_INDEX_DIR.")
        else:
            # Titan V2 returns unit vectors when normalize=true, so inner product == cosine.
            self.store = FAISS(embedding_function=embeddings,
                               index=faiss.IndexFlatIP(settings.embed_dimensions),
                               docstore=InMemoryDocstore(), index_to_docstore_id={},
                               distance_strategy=DistanceStrategy.MAX_INNER_PRODUCT)
            self.manifest = {"embedding_model": settings.embed_model_id,
                             "embedding_dimensions": settings.embed_dimensions,
                             "incidents": {}, "s3_objects": {}}

    def is_current(self, incident: str, content_hash: str) -> bool:
        entry = self.manifest["incidents"].get(incident)
        return bool(entry and entry["content_hash"] == content_hash)

    def source_unchanged(self, source: Source) -> bool:
        return bool(source.version) and self.manifest["s3_objects"].get(source.name) == source.version

    def mark_source_done(self, source: Source) -> None:
        if source.version:
            self.manifest["s3_objects"][source.name] = source.version

    def upsert(self, incident: str, content_hash: str, docs: list[Document],
               vectors: list[list[float]]) -> None:
        old_ids = self.manifest["incidents"].get(incident, {}).get("chunk_ids", [])
        existing = set(self.store.index_to_docstore_id.values())
        live_old = [i for i in old_ids if i in existing]
        if live_old:
            self.store.delete(ids=live_old)
        ids = [d.id for d in docs]
        self.store.add_embeddings(text_embeddings=list(zip((d.page_content for d in docs), vectors)),
                                  metadatas=[d.metadata for d in docs], ids=ids)
        self.manifest["incidents"][incident] = {
            "content_hash": content_hash,
            "chunk_ids": ids,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }

    def save(self) -> None:
        """Write to a sibling temp dir, then swap it in so readers never see a half-written index."""
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.dir.with_name(f".{self.dir.name}.tmp-{os.getpid()}")
        old = self.dir.with_name(f".{self.dir.name}.old-{os.getpid()}")
        shutil.rmtree(tmp, ignore_errors=True)
        self.manifest["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.manifest["chunk_count"] = self.store.index.ntotal
        self.store.save_local(str(tmp), index_name=self.s.index_name)
        (tmp / self.MANIFEST).write_text(json.dumps(self.manifest, indent=2))
        if self.dir.exists():
            self.dir.rename(old)
        tmp.rename(self.dir)
        shutil.rmtree(old, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
@dataclass
class Stats:
    files: int = 0
    files_unchanged: int = 0
    records: int = 0
    records_with_pii_warning: int = 0
    skipped_unchanged: int = 0
    incidents_indexed: int = 0
    chunks_embedded: int = 0
    failures: int = 0
    embed_seconds: float = 0.0
    chunk_types: dict = field(default_factory=dict)


def make_embeddings(settings: Settings) -> BedrockEmbeddings:
    client = boto3.client(
        "bedrock-runtime", region_name=settings.aws_region,
        config=Config(retries={"max_attempts": settings.bedrock_max_attempts, "mode": "adaptive"},
                      connect_timeout=10, read_timeout=60))
    return BedrockEmbeddings(client=client, model_id=settings.embed_model_id,
                             dimensions=settings.embed_dimensions, normalize=True,
                             model_kwargs={"normalize": True})


def run(args: argparse.Namespace, settings: Settings) -> int:
    run_id = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"
    failures = FailureSink(settings, run_id)
    stats = Stats()
    started = time.monotonic()

    splitter = RecursiveCharacterTextSplitter(chunk_size=settings.narrative_max_chars,
                                              chunk_overlap=settings.narrative_overlap_chars,
                                              separators=["\n", ". ", " ", ""])
    embeddings = make_embeddings(settings)
    index = None if args.dry_run else IncidentIndex(settings, embeddings)
    dirty = 0
    aborted = False

    if args.input:
        input_desc = args.input
        sources = iter_local_sources([Path(p) for p in args.input])
    else:
        input_desc = f"s3://{settings.clean_bucket}/{settings.clean_prefix}"
        s3 = boto3.client("s3", region_name=settings.aws_region)
        sources = iter_s3_sources(s3, settings.clean_bucket, settings.clean_prefix)
    LOG.info("run %s start input=%s index=%s model=%s dim=%d pii_guard=%s dry_run=%s", run_id,
             input_desc, settings.index_dir, settings.embed_model_id, settings.embed_dimensions,
             settings.pii_guard, args.dry_run)

    try:
        for source in sources:
            stats.files += 1
            if index and not args.force and index.source_unchanged(source):
                stats.files_unchanged += 1
                continue
            try:
                records = source.load()
            except (OSError, ValueError, ClientError, BotoCoreError) as exc:
                failures.record(None, "unreadable_input_file", exc, source.name)
                continue

            failures_before = failures.count

            for record in records:
                stats.records += 1
                inc = record.get("incident_number") if isinstance(record, dict) else None
                if not inc:
                    failures.record(None, "missing_incident_number", source=source.name)
                    continue
                try:
                    chash = content_hash_of(record)
                    if index and not args.force and index.is_current(inc, chash):
                        stats.skipped_unchanged += 1
                        continue

                    docs = build_chunks(record, chash, source.name, settings, splitter)
                    if not docs:
                        failures.record(inc, "no_embeddable_content", source=source.name)
                        continue

                    if settings.pii_guard != "off":
                        pii = sorted({t for d in docs for t in find_residual_pii(d.page_content)})
                        if pii and settings.pii_guard == "block":
                            failures.record(inc, f"residual_pii_detected:{','.join(pii)}",
                                            source=source.name)
                            continue
                        if pii:
                            stats.records_with_pii_warning += 1
                            LOG.warning("residual PII pattern(s) in incident=%s types=%s; "
                                        "indexing anyway (DAIMO_PII_GUARD=warn)", inc, ",".join(pii))

                    if args.dry_run:
                        for d in docs:
                            print(f"--- {d.id} ({len(d.page_content)} chars)\n{d.page_content}\n")
                        stats.incidents_indexed += 1
                        continue

                    t0 = time.monotonic()
                    vectors = embeddings.embed_documents([d.page_content for d in docs])
                    stats.embed_seconds += time.monotonic() - t0

                    index.upsert(inc, chash, docs, vectors)
                    stats.incidents_indexed += 1
                    stats.chunks_embedded += len(docs)
                    for d in docs:
                        ct = d.metadata["chunk_type"]
                        stats.chunk_types[ct] = stats.chunk_types.get(ct, 0) + 1
                    dirty += 1
                    if dirty >= settings.checkpoint_every:
                        index.save()
                        dirty = 0
                except ClientError as exc:
                    code = exc.response.get("Error", {}).get("Code", "")
                    failures.record(inc, f"bedrock_error:{code}", exc, source.name)
                    if code in FATAL_BEDROCK_ERRORS:
                        LOG.critical("non-retryable Bedrock error (%s); aborting run", code)
                        aborted = True
                        return 2
                except Exception as exc:  # noqa: BLE001 - every record failure must be captured
                    failures.record(inc, "chunk_embed_error", exc, source.name)

            # Only skip this object next run if every record in it made it into the index.
            if index and failures.count == failures_before:
                index.mark_source_done(source)
                dirty += 1
    except FileNotFoundError as exc:
        failures.record(None, "input_path_missing", exc)
    except (ClientError, BotoCoreError) as exc:
        failures.record(None, "s3_list_error", exc, input_desc)
        aborted = True
    finally:
        if index and dirty:
            index.save()
        stats.failures = failures.count
        summary = {"event": "run_summary", "run_id": run_id, "aborted": aborted,
                   "duration_seconds": round(time.monotonic() - started, 2),
                   "index_chunk_total": index.store.index.ntotal if index else None,
                   **{k: (round(v, 2) if isinstance(v, float) else v)
                      for k, v in stats.__dict__.items()}}
        LOG.info("run_summary %s", json.dumps(summary))

    return 1 if failures.count else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--input", action="append",
                        help="local file or directory of .json/.jsonl records (repeatable); "
                             "overrides the S3 clean bucket")
    parser.add_argument("--dry-run", action="store_true",
                        help="build and PII-check chunks, print them; no Bedrock calls or index writes")
    parser.add_argument("--force", action="store_true",
                        help="re-read every S3 object and re-embed incidents even if unchanged")
    parser.add_argument("--log-level", default=os.environ.get("DAIMO_LOG_LEVEL", "INFO"))
    args = parser.parse_args()

    logging.basicConfig(level=args.log_level.upper(), stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # langchain_aws logs every call at INFO and re-logs errors we already capture.
    logging.getLogger("langchain_aws").setLevel(logging.CRITICAL)

    settings = Settings()

    lock_path = settings.index_dir.with_name(f".{settings.index_dir.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            LOG.warning("another chunk_embed run holds %s; exiting", lock_path)
            return 0
        return run(args, settings)


if __name__ == "__main__":
    sys.exit(main())
