"""Inventory MT survey source files without changing data or selecting recordings.

Text formats are preview classifications, not full-file QC. Filename dates are
unverified and have no assigned timezone. MiniSEED timing is read from headers.
No calibration, ownership assignment, concatenation, renaming or deletion occurs.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import os
import re
import stat
import sys
import warnings
from collections import Counter, defaultdict
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from inventory_reports import write_reports, migrate_inventory
from survey_settings import canonical_station
from inventory_parallel import scan_sources

EM = ("ex", "ey", "bx", "by", "bz")
FILE_COLUMNS = (
    "source_role", "source_label", "relative_path", "entry_type", "bytes",
    "filename_prefix", "filename_time_unverified", "prefix_relation",
    "extension", "channel_from_extension", "format", "evidence",
    "header_trace_count", "header_sample_rates_hz", "header_start_utc",
    "header_last_sample_utc", "header_sample_count_sum", "review_flags",
)
GROUP_COLUMNS = (
    "source_role", "source_label", "group_key", "filename_prefix",
    "filename_time_unverified", "prefix_relation", "em_file_count",
    "channels_present", "missing_channel_names", "repeated_channel_names",
    "formats", "same_stem_in_other_sources", "review_flags",
)


def save_json(path, value):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def save_csv(path, columns, rows):
    with path.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def filename_info(stem, label):
    match = re.fullmatch(r"(.*?)(\d{12})", stem)
    if not match:
        return "", "", "no_timestamp_suffix"
    prefix = match[1].rstrip("_-")
    try:
        # Python's %y convention: 00..68 => 2000..2068; 69..99 => 1969..1999.
        stamp = datetime.strptime(match[2], "%y%m%d%H%M%S").isoformat()
    except ValueError:
        return prefix, "", "invalid_filename_date"
    if prefix == label:
        relation = "matches_folder_label"
    elif prefix.casefold() == label.casefold():
        relation = "matches_ignoring_case"
    else:
        relation = "different_prefix_requires_evidence"
    return prefix, stamp, relation


def preview_format(data, whole_file):
    if not data:
        return "empty", "No bytes in file"
    if (len(data) >= 48 and re.fullmatch(rb"[0-9 ]{6}[DRQM] ", data[:8])):
        return "miniseed2_candidate", "SEED sequence and data-quality signature"
    if data.startswith(b"MS\x03"):
        return "miniseed3_candidate", "MiniSEED 3 signature; no decoder used here"
    if data.startswith(b"PK\x03\x04"):
        return "zip_signature", "ZIP signature; archive not opened"
    if data.startswith(b"\x89HDF\r\n\x1a\n"):
        return "hdf5_signature", "HDF5 signature; file not opened as HDF5"
    if b"\x00" in data:
        return "binary_unidentified", "Binary bytes in preview"
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        return "non_ascii_preview", "Preview is not ASCII; encoding/format not assigned"
    lines = text.splitlines()
    if not whole_file and not text.endswith(("\n", "\r")):
        lines = lines[:-1]  # Do not classify a partial final line.
    lines = [line.strip() for line in lines if line.strip()]
    if lines and all(re.fullmatch(r"[+-]?\d+", line) for line in lines):
        return "integer_text_preview", "Complete nonblank preview lines contain one integer each"
    number = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?"
    if lines and all(re.fullmatch(number, line) for line in lines):
        return "numeric_text_preview", "Complete nonblank preview lines contain one number each"
    return "ascii_text_preview", "ASCII preview; not a one-number-per-line sample preview"


def miniseed_headers(path):
    from obspy import read
    records = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        # File object prevents wildcard expansion in source filenames.
        with path.open("rb") as stream:
            traces = read(stream, format="MSEED", headonly=True,
                          apply_calib=False, check_compression=False)
        for trace in traces:
            rate, count = float(trace.stats.sampling_rate), int(trace.stats.npts)
            if not math.isfinite(rate) or rate <= 0 or count <= 0:
                raise ValueError("Invalid rate or sample count in MiniSEED header")
            records.append({
                "seed_id": trace.id, "sample_rate_hz": rate, "samples": count,
                "start_utc": str(trace.stats.starttime),
                "last_sample_utc": str(trace.stats.endtime),
                "end_exclusive_utc": str(trace.stats.starttime + count / rate),
                "channel_in_header": trace.stats.channel,
            })
    if not records:
        raise ValueError("No trace headers returned")
    return records, [str(item.message) for item in caught]


def scan_file(path, root, role, label, preview_bytes, mseed_limit, headers):
    row = {key: "" for key in FILE_COLUMNS}
    relative = path.relative_to(root).as_posix()
    component = path.suffix[1:].lower()
    prefix, stamp, relation = filename_info(path.stem, label)
    row.update(source_role=role, source_label=label, relative_path=relative,
               extension=path.suffix, channel_from_extension=component if component in EM else "",
               filename_prefix=prefix, filename_time_unverified=stamp,
               prefix_relation=relation)
    flags = []
    try:
        before = path.lstat()
        row["bytes"] = before.st_size
        if stat.S_ISLNK(before.st_mode):
            row.update(entry_type="nested_symlink_not_followed", format="not_read")
            flags.append("nested_symlink_not_followed")
        elif not stat.S_ISREG(before.st_mode):
            row.update(entry_type="special_entry_not_read", format="not_read")
            flags.append("special_entry_not_read")
        else:
            row["entry_type"] = "regular_file"
            with path.open("rb") as stream:
                data = stream.read(preview_bytes)
            kind, evidence = preview_format(data, before.st_size <= preview_bytes)
            row.update(format=kind, evidence=evidence)
            if kind == "miniseed2_candidate":
                if before.st_size > mseed_limit:
                    flags.append("miniseed_header_scan_size_limit")
                else:
                    try:
                        traces, notes = miniseed_headers(path)
                        headers[f"{role}/{label}/{relative}"] = {"traces": traces, "warnings": notes}
                        row.update(format="miniseed_headers_read",
                                   evidence="ObsPy headonly read; sample payload not decoded",
                                   header_trace_count=len(traces),
                                   header_sample_rates_hz=";".join(str(x) for x in sorted({t["sample_rate_hz"] for t in traces})),
                                   header_start_utc=min(t["start_utc"] for t in traces),
                                   header_last_sample_utc=max(t["last_sample_utc"] for t in traces),
                                   header_sample_count_sum=sum(t["samples"] for t in traces))
                        if notes:
                            flags.append("miniseed_parser_warning")
                        if len(traces) > 1:
                            flags.append("multiple_header_traces_review_timing")
                        if component in EM and any(t["channel_in_header"].lower() != component for t in traces):
                            flags.append("header_channel_differs_from_extension")
                    except Exception as exc:
                        flags.append("miniseed_header_scan_failed")
                        headers[f"{role}/{label}/{relative}"] = {"error": str(exc)}
            if component in EM and row["format"] not in ("integer_text_preview", "numeric_text_preview", "miniseed_headers_read"):
                flags.append("em_file_format_requires_review")
            after = path.lstat()
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
                flags.append("source_changed_during_scan")
    except OSError as exc:
        row["format"] = "unreadable"
        flags.append("io_error: " + str(exc))
    if component in EM and relation not in ("matches_folder_label", "matches_ignoring_case"):
        flags.append(relation)
    row["review_flags"] = "; ".join(flags)
    return row


def build_groups(files):
    grouped = defaultdict(list)
    for row in files:
        if row["channel_from_extension"] in EM:
            key = str(Path(row["relative_path"]).with_suffix("")).replace("\\", "/")
            grouped[row["source_role"], row["source_label"], key].append(row)
    stems = defaultdict(list)
    for key in grouped:
        stems[Path(key[2]).name.casefold()].append(key)
    result = []
    for key, members in sorted(grouped.items()):
        counts = Counter(row["channel_from_extension"] for row in members)
        missing = [c for c in EM if counts[c] == 0]
        repeated = [c for c in EM if counts[c] > 1]
        matches = ["/".join(other) for other in stems[Path(key[2]).name.casefold()]
                   if other[:2] != key[:2]]
        flags = []
        if missing:
            flags.append("missing_channel_names")
        if repeated:
            flags.append("repeated_channel_names")
        if matches:
            flags.append("name_match_only_not_verified_duplicate")
        if any(row["review_flags"] for row in members):
            flags.append("see_file_review_flags")
        first = members[0]
        result.append(dict(source_role=key[0], source_label=key[1], group_key=key[2],
                           filename_prefix=first["filename_prefix"],
                           filename_time_unverified=first["filename_time_unverified"],
                           prefix_relation=first["prefix_relation"], em_file_count=len(members),
                           channels_present=";".join(c for c in EM if counts[c]),
                           missing_channel_names=";".join(missing),
                           repeated_channel_names=";".join(repeated),
                           formats=";".join(sorted({r["format"] for r in members})),
                           same_stem_in_other_sources=";".join(matches), review_flags="; ".join(flags)))
    return result


def inventory(stations, references, out, preview_bytes=4096, mseed_limit=64 * 1024**2):
    sources = []
    for role, paths in (("survey", stations), ("reference", references)):
        for source in paths:
            source = Path(source).absolute()
            root = source.resolve(strict=True)
            if not root.is_dir():
                raise ValueError(f"Source is not a directory: {source}")
            if any(root == item["root"] or root in item["root"].parents or item["root"] in root.parents for item in sources):
                raise ValueError("Duplicate or overlapping source directories")
            if any(canonical_station(source.name).casefold() == item["label"].casefold() for item in sources):
                raise ValueError("Source folder labels must be unique ignoring case")
            sources.append(dict(role=role, label=canonical_station(source.name), link=source, root=root))
    out = Path(out).absolute()
    target = out.resolve()
    if any(target == s["root"] or s["root"] in target.parents for s in sources):
        raise ValueError("Reports must be outside every source directory")
    out.mkdir(parents=True, exist_ok=False)
    files, headers, walk_errors = scan_sources(sources, preview_bytes, mseed_limit)
    groups = build_groups(files)
    complete = not walk_errors and not any("io_error:" in row["review_flags"] or "source_changed_during_scan" in row["review_flags"] for row in files)
    summary = dict(schema="mtl0.inventory/1", inventory_complete=complete,
                   decisions_applied=False, source_data_modified=False,
                   created_utc=datetime.now(timezone.utc).isoformat(),
                   script_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                   python=sys.version.split()[0], preview_bytes=preview_bytes,
                   miniseed_header_scan_limit_bytes=mseed_limit, sources=[], walk_errors=walk_errors,
                   limitations=[
                       "Text classifications inspect only the initial preview; no full sample counts or full-file hashes are calculated.",
                       "Filename times are unverified, timezone-free YYMMDDhhmmss interpretations using Python's %y century rule.",
                       "MiniSEED headers are read without decoding samples. Header sample sums are not counts of unique time samples.",
                       "A filename extension or folder label does not prove channel or station ownership.",
                       "A matching name is not proof of duplicate data. No station assignment, exclusions, calibration, renaming or concatenation is performed.",
                       "Channel completeness refers to names present in each exact file-stem group, not waveform integrity or final runs.",
                       "Top-level source links are resolved deliberately; nested links are listed but not followed.",
                   ])
    try:
        summary["obspy"] = version("obspy")
    except PackageNotFoundError:
        summary["obspy"] = "unavailable; MiniSEED candidates require later header inspection"
    for source in sources:
        rows = [r for r in files if r["source_label"] == source["label"]]
        gr = [r for r in groups if r["source_label"] == source["label"]]
        info = dict(role=source["role"], label=source["label"], input_link=str(source["link"]),
                    resolved_root=str(source["root"]), entries=len(rows),
                    regular_file_bytes=sum(r["bytes"] for r in rows if r["entry_type"] == "regular_file"),
                    em_filename_groups=len(gr), formats=dict(Counter(r["format"] for r in rows)),
                    groups_missing_channel_names=sum(bool(r["missing_channel_names"]) for r in gr),
                    groups_with_name_matches_elsewhere=sum(bool(r["same_stem_in_other_sources"]) for r in gr))
        summary["sources"].append(info)
        print(f"{source['label']}: {len(rows)} entries; {len(gr)} EM filename groups; formats: {info['formats']}", flush=True)
    write_reports(out, summary, files, groups, headers)
    print(("SUCCESS" if complete else "INCOMPLETE") + f": inventory reports written to {out}", flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--station", type=Path, action="append", default=[])
    parser.add_argument("--from-inventory", type=Path, help="Reformat an existing inventory without reading raw data")
    parser.add_argument("--reference", type=Path, action="append", default=[])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--preview-bytes", type=int, default=4096)
    parser.add_argument("--miniseed-limit-mib", type=int, default=64)
    args = parser.parse_args()
    if not 64 <= args.preview_bytes <= 1048576 or not 1 <= args.miniseed_limit_mib <= 256:
        parser.error("Preview must be 64..1048576 bytes and MiniSEED limit 1..256 MiB")
    if args.from_inventory:
        if args.station or args.reference:
            parser.error('--from-inventory cannot be combined with source folders')
        result = migrate_inventory(args.from_inventory, args.out)
        return 0 if result['inventory_complete'] else 2
    if not args.station:
        parser.error('Provide at least one --station or --from-inventory')
    result = inventory(args.station, args.reference, args.out, args.preview_bytes, args.miniseed_limit_mib * 1024**2)
    return 0 if result["inventory_complete"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
