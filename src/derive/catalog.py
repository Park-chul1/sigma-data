"""Canonical normalized CIZ discovery and content-addressed local provenance.

Never discovers demo/sample directories and never changes RAW/NORMALIZED files.
SHA256 is streamed once per file identity; stat/ctime changes invalidate the local
hash index. This protects against ordinary edits, not hostile filesystem tampering.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def sql_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def parquet_sql(paths):
    return "read_parquet([" + ",".join(sql_literal(p) for p in paths) + "], hive_partitioning=false)"


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        json.dump(value, f, indent=2, sort_keys=True, default=str, allow_nan=False)
        f.write("\n")
        temp = Path(f.name)
    os.replace(temp, path)


class ContentHashes:
    def __init__(self, path):
        self.path = Path(path)
        self.values = json.loads(self.path.read_text()) if self.path.exists() else {}

    def file(self, path):
        path = Path(path).resolve()
        stat = path.stat()
        identity = [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino, stat.st_dev]
        old = self.values.get(str(path))
        if old and old["identity"] == identity:
            return old["sha256"]
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                h.update(block)
        # Do not accept a file changed while it was being fingerprinted.
        after = path.stat()
        if identity != [after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_ino, after.st_dev]:
            raise RuntimeError(f"Source changed during hashing: {path}")
        result = h.hexdigest()
        self.values[str(path)] = {"identity": identity, "sha256": result}
        return result

    def save(self):
        write_json(self.path, self.values)

    def assert_unchanged(self, paths):
        for path in paths:
            path = Path(path).resolve()
            stat = path.stat()
            identity = [stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino,stat.st_dev]
            expected = self.values.get(str(path))
            if expected is None or expected["identity"] != identity:
                raise RuntimeError(f"Source changed during build: {path}")


class NormalizedCatalog:
    def __init__(self, data_root, hash_index):
        self.root = Path(data_root).resolve()
        self.base = self.root / "normalized/crsp"
        self.hashes = hash_index
        self.daily = sorted((self.base / "daily").glob("year=*/month=*/part-000.parquet"))
        self.info = self.base / "security_info/part-000.parquet"
        self.delistings = self.base / "delistings/part-000.parquet"
        if not self.daily or not self.info.is_file() or not self.delistings.is_file():
            raise FileNotFoundError(f"Canonical daily/security_info/delistings missing under {self.base}")
        self.months = {}
        inventory = []
        all_manifests = []
        for path, table in [(p, "stkdlysecurityprimarydata") for p in self.daily] + [
            (self.info, "stksecurityinfohist"), (self.delistings, "stkdelists")
        ]:
            manifest_path = path.parent / "_SUCCESS.json"
            if not manifest_path.is_file():
                raise RuntimeError(f"Uncommitted normalized input: {path}")
            manifest = json.loads(manifest_path.read_text())
            # The actual table lineage is checked, not just normalized column names.
            source = Path(manifest.get("source", ""))
            if table not in source.parts or manifest.get("schema_version") != 1:
                raise RuntimeError(f"Unsupported/unknown normalized source or schema: {manifest_path}")
            for file in (path, manifest_path):
                inventory.append({"path": str(file.relative_to(self.root)),
                                  "size": file.stat().st_size, "sha256": hash_index.file(file)})
            raw_base = self.root / "raw/crsp" / table
            raw_manifest = raw_base / "_SUCCESS.json"
            if table == "stkdlysecurityprimarydata":
                raw_manifest = raw_base / path.parent.parent.name / path.parent.name / "_SUCCESS.json"
                key = (int(path.parent.parent.name.split("=")[1]), int(path.parent.name.split("=")[1]))
                self.months[key] = (path, manifest)
                if not manifest.get("min_date") or not manifest.get("max_date"):
                    raise RuntimeError(f"Missing normalized coverage: {manifest_path}")
            if raw_manifest.exists():
                raw = json.loads(raw_manifest.read_text())
                if raw.get("source_table") != table or raw.get("source_schema") != "crsp":
                    raise RuntimeError(f"Conflicting raw lineage: {raw_manifest}")
                inventory.append({"path": str(raw_manifest.relative_to(self.root)),
                                  "size": raw_manifest.stat().st_size, "sha256": hash_index.file(raw_manifest)})
                all_manifests.append({"path": str(raw_manifest.relative_to(self.root)), "metadata": raw})
        self.source_start = min(m["min_date"] for _, m in self.months.values())
        self.source_end = max(m["max_date"] for _, m in self.months.values())
        self.snapshot = {"format": "CRSP_CIZ", "normalized_schema_version": 1,
                         "inventory": inventory, "raw_manifests": all_manifests,
                         "source_start": self.source_start, "source_end": self.source_end,
                         "vendor_release": None,
                         "temporal_provenance": {
                             "source_vintage": None,
                             "source_vintage_status": "UNKNOWN_NO_HISTORICAL_REVISION_VINTAGES",
                             "ingested_at": [
                                 {"manifest_path": item["path"],
                                  "source_table": item["metadata"]["source_table"],
                                  "timestamp": item["metadata"].get("downloaded_at_utc"),
                                  "basis": "RAW_MANIFEST_DOWNLOADED_AT_UTC"}
                                 for item in all_manifests
                             ],
                             "ingested_at_status": "RECORDED_RAW_COLLECTION_TIMES_ONLY",
                             "effective_date_basis": "SOURCE_OBSERVATION_OR_VALIDITY_DATE",
                             "available_at_basis": "ASSUMED_SESSION_LAG_NOT_PUBLICATION_TIME",
                             "normalized_at_is_publication_time": False,
                         },
                         "identity_method": "SHA256; cached by size/mtime_ns/ctime_ns/inode/device"}
        self.snapshot_id = digest(self.snapshot)
        self.hashes.save()

    def assert_unchanged(self):
        self.hashes.assert_unchanged(self.root / item["path"] for item in self.snapshot["inventory"])

    def selected_months(self, start, end):
        keys = []
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            if (year, month) not in self.months:
                raise RuntimeError(f"Missing source partition {year}-{month:02d}")
            keys.append((year, month))
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return keys


def code_identity(repo_root):
    root = Path(repo_root)
    paths = sorted((root / "src/derive").glob("*.py"))
    paths += [root / "scripts/build_backtest_dataset.py", root / "requirements-research.txt"]
    return digest({str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths})


def verify_artifacts(folder, hashes):
    folder = Path(folder)
    manifest_path = folder / "_SUCCESS.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"Uncommitted derived output: {folder}")
    try:
        manifest = json.loads(manifest_path.read_text())
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError(f"Derived manifest changed/corrupt: {manifest_path}") from exc
    artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if not isinstance(artifacts, dict) or not artifacts:
        raise RuntimeError(f"Derived artifact inventory missing/corrupt: {manifest_path}")
    for name, fingerprint in artifacts.items():
        if (not isinstance(name, str) or Path(name).name != name or not name.endswith(".parquet")
                or not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None):
            raise RuntimeError(f"Derived artifact inventory changed/corrupt: {manifest_path}")
    names = set(artifacts)
    # A removed checksum must not make a damaged/missing result reusable. Run
    # row counts and declared roles independently describe the output contract.
    if "run_id" in manifest:
        rows = manifest.get("rows")
        roles = manifest.get("artifact_roles")
        if (not isinstance(rows, dict) or set(rows) != names
                or not isinstance(roles, dict) or not set(roles) <= names):
            raise RuntimeError(f"Derived artifact inventory changed/corrupt: {manifest_path}")
    elif "month" in manifest:
        if not {"panel.parquet", "events.parquet", "terminal_reconciliation.parquet"} <= names:
            raise RuntimeError(f"Derived artifact inventory changed/corrupt: {manifest_path}")
    elif "context" in manifest:
        if names != {"factors.parquet"}:
            raise RuntimeError(f"Derived artifact inventory changed/corrupt: {manifest_path}")
    if names != {path.name for path in folder.glob("*.parquet")}:
        raise RuntimeError(f"Derived artifact inventory changed/corrupt: {folder}")
    for name, fingerprint in artifacts.items():
        if not (folder / name).is_file() or hashes.file(folder / name) != fingerprint:
            raise RuntimeError(f"Derived artifact changed/corrupt: {folder / name}")
    return manifest
