#!/usr/bin/env python3
"""Objective forensic metadata/metrics for captured face videos (stdlib only).

This tool deliberately does NOT decide whether a face or liveness check passed.
Evidence defaults to the system temporary directory, never the source checkout.
"""

import argparse
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import statistics
import subprocess
import sys
import tempfile

VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"}
FILTER_NAMES = (
    "blackdetect", "freezedetect", "blurdetect", "metadata",
    "fps", "select", "scale", "tile",
)
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
REPO_ROOT = Path(__file__).resolve().parent.parent


class CommandError(Exception):
    pass


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def parse_float(value):
    try:
        if value is None or value in ("", "N/A"):
            return None
        answer = float(value)
        return answer if math.isfinite(answer) else None
    except (TypeError, ValueError):
        return None


def parse_int(value):
    try:
        if value is None or value in ("", "N/A"):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_rate(value):
    if not value or value in ("0/0", "N/A"):
        return None
    if "/" in str(value):
        numerator, denominator = str(value).split("/", 1)
        top, bottom = parse_float(numerator), parse_float(denominator)
        if top is None or not bottom:
            return None
        return top / bottom
    return parse_float(value)


def run_command(command, timeout):
    try:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return {
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "timed_out": False,
            "exception": None,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
            "timed_out": True,
            "exception": "timeout after {} seconds".format(timeout),
        }
    except OSError as exc:
        return {
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "exception": str(exc),
        }


def result_text(result):
    if isinstance(result.get("stdout"), bytes):
        result["stdout"] = result["stdout"].decode("utf-8", "replace")
    if isinstance(result.get("stderr"), bytes):
        result["stderr"] = result["stderr"].decode("utf-8", "replace")
    return (result.get("stderr") or "") + (result.get("stdout") or "")


def result_tail(result, limit=6000):
    text = result_text(result).strip()
    return text[-limit:] if text else (result.get("exception") or "")


def save_log(path, command, result):
    payload = [
        "$ " + " ".join(str(part) for part in command),
        "returncode={} timed_out={} exception={}".format(
            result.get("returncode"), result.get("timed_out"), result.get("exception")
        ),
        result_text(result),
    ]
    path.write_text("\n".join(payload), encoding="utf-8", errors="replace")


def resolve_executable(value, required=True):
    if not value:
        return None
    candidate = Path(value).expanduser()
    if candidate.is_file() and os.access(str(candidate), os.X_OK):
        return str(candidate.resolve())
    found = shutil.which(str(value))
    if found:
        return found
    if required:
        raise CommandError("executable not found or not executable: {}".format(value))
    return None


def default_ffprobe():
    configured = os.environ.get("FACECAP_FFPROBE")
    if configured:
        return resolve_executable(configured, required=True)
    fixed = Path("/root/ffprobe")
    if fixed.is_file() and os.access(str(fixed), os.X_OK):
        return str(fixed)
    return resolve_executable("ffprobe", required=False)


def tool_version(executable, timeout):
    if not executable:
        return None
    result = run_command([executable, "-version"], timeout)
    if result.get("returncode") != 0:
        return "unavailable: " + result_tail(result, 500)
    lines = (result.get("stdout") or "").splitlines()
    return lines[0] if lines else "unavailable: empty version output"


def inspect_filters(ffmpeg, timeout):
    result = run_command([ffmpeg, "-hide_banner", "-filters"], timeout)
    if result.get("returncode") != 0:
        return {
            "status": "unavailable",
            "reason": "cannot list ffmpeg filters: " + result_tail(result, 1000),
            "filters": {name: False for name in FILTER_NAMES},
        }
    text = result_text(result)
    return {
        "status": "ok",
        "filters": {
            name: any(name in line.split() for line in text.splitlines())
            for name in FILTER_NAMES
        },
    }


def unavailable(reason, **extra):
    answer = {"status": "unavailable", "reason": reason}
    answer.update(extra)
    return answer


def ffprobe_metadata(ffprobe, path, timeout):
    entries = (
        "stream=index,codec_type,codec_name,codec_long_name,profile,width,height,"
        "pix_fmt,r_frame_rate,avg_frame_rate,nb_frames,nb_read_frames,duration:"
        "format=format_name,duration,size"
    )
    command = [
        ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
        "-show_entries", entries, "-of", "json", str(path),
    ]
    result = run_command(command, timeout)
    if result.get("returncode") != 0:
        return None, "ffprobe failed: " + result_tail(result, 1500)
    try:
        payload = json.loads(result.get("stdout") or "")
        streams = payload.get("streams") or []
        if not streams:
            raise ValueError("no video stream returned by ffprobe")
        stream = streams[0]
        fmt = payload.get("format") or {}
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        return None, "invalid ffprobe JSON: {}".format(exc)

    counted = parse_int(stream.get("nb_read_frames"))
    declared = parse_int(stream.get("nb_frames"))
    duration = parse_float(stream.get("duration"))
    if duration is None:
        duration = parse_float(fmt.get("duration"))
    rate = parse_rate(stream.get("avg_frame_rate"))
    if rate is None:
        rate = parse_rate(stream.get("r_frame_rate"))
    metadata = {
        "status": "ok",
        "source": "ffprobe",
        "codec": stream.get("codec_name"),
        "codec_long_name": stream.get("codec_long_name"),
        "profile": stream.get("profile"),
        "width": parse_int(stream.get("width")),
        "height": parse_int(stream.get("height")),
        "pixel_format": stream.get("pix_fmt"),
        "duration_seconds": duration,
        "frame_count": counted if counted is not None else declared,
        "frame_count_source": (
            "ffprobe.nb_read_frames" if counted is not None
            else "ffprobe.nb_frames" if declared is not None else None
        ),
        "frame_rate": rate,
        "frame_rate_source": (
            "ffprobe.avg_frame_rate" if parse_rate(stream.get("avg_frame_rate")) is not None
            else "ffprobe.r_frame_rate" if rate is not None else None
        ),
        "format_name": fmt.get("format_name"),
        "notes": [],
    }
    return metadata, None


def ffmpeg_metadata(ffmpeg, path, timeout, reason=None):
    command = [ffmpeg, "-nostdin", "-hide_banner", "-i", str(path)]
    result = run_command(command, timeout)
    text = result_text(result)
    video_match = re.search(r"Stream[^\n]*Video:\s*([^\s,(]+)[^\n]*", text)
    video_line = video_match.group(0) if video_match else ""
    if not video_match:
        return {
            "status": "error", "source": "ffmpeg -i",
            "reason": "no video stream metadata: " + (result_tail(result, 2000) or "no output"),
            "notes": [reason] if reason else [],
        }
    duration = None
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
    if duration_match:
        hours, minutes, seconds = duration_match.groups()
        duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    dimensions = re.search(r"\b([1-9]\d{0,4})x([1-9]\d{0,4})\b", video_line)
    rate_match = re.search(r"(" + NUMBER + r")\s*fps\b", video_line)
    notes = [reason] if reason else []
    notes.append("ffprobe unavailable/failed; codec, dimensions, duration and fps parsed from ffmpeg")
    return {
        "status": "degraded",
        "source": "ffmpeg -i",
        "codec": video_match.group(1),
        "codec_long_name": None,
        "profile": None,
        "width": int(dimensions.group(1)) if dimensions else None,
        "height": int(dimensions.group(2)) if dimensions else None,
        "pixel_format": None,
        "duration_seconds": duration,
        "frame_count": None,
        "frame_count_source": None,
        "frame_rate": float(rate_match.group(1)) if rate_match else None,
        "frame_rate_source": "ffmpeg -i" if rate_match else None,
        "format_name": None,
        "notes": notes,
    }


def get_metadata(ffprobe, ffmpeg, path, timeout):
    if ffprobe:
        metadata, error = ffprobe_metadata(ffprobe, path, timeout)
        if metadata is not None:
            return metadata
        return ffmpeg_metadata(ffmpeg, path, timeout, error)
    return ffmpeg_metadata(
        ffmpeg, path, timeout,
        "ffprobe not installed; use --ffprobe /root/ffprobe for complete metadata",
    )


def count_decoded_frames(ffmpeg, path, timeout):
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
        "-progress", "pipe:1", "-nostats", "-i", str(path),
        "-map", "0:v:0", "-an", "-sn", "-dn", "-f", "null", "-",
    ]
    result = run_command(command, timeout)
    frames = [int(item) for item in re.findall(r"(?m)^frame=\s*(\d+)\s*$", result.get("stdout") or "")]
    if result.get("returncode") != 0:
        return unavailable("frame decode/count failed: " + result_tail(result, 2000)), command, result
    if not frames:
        return unavailable("ffmpeg progress emitted no frame count"), command, result
    return {
        "status": "ok", "frame_count": frames[-1],
        "frame_count_source": "ffmpeg decoded progress",
    }, command, result


def black_metric(ffmpeg, filters, path, timeout, args, log_path):
    settings = {
        "minimum_duration_seconds": args.black_duration,
        "pixel_threshold": args.black_pixel_threshold,
    }
    if not filters.get("blackdetect"):
        return unavailable("ffmpeg filter blackdetect is unavailable", **settings)
    graph = "blackdetect=d={}:pix_th={}".format(args.black_duration, args.black_pixel_threshold)
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info", "-i", str(path),
        "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", graph, "-f", "null", "-",
    ]
    result = run_command(command, timeout)
    save_log(log_path, command, result)
    if result.get("returncode") != 0:
        return unavailable("blackdetect failed: " + result_tail(result, 2000), **settings)
    text = result_text(result)
    pattern = re.compile(
        r"black_start:\s*(" + NUMBER + r")\s+black_end:\s*(" + NUMBER +
        r")\s+black_duration:\s*(" + NUMBER + r")"
    )
    intervals = [
        {"start": float(match.group(1)), "end": float(match.group(2)),
         "duration": float(match.group(3))}
        for match in pattern.finditer(text)
    ]
    return {
        "status": "ok", **settings, "intervals": intervals,
        "total_seconds": sum(item["duration"] for item in intervals),
        "event_count": len(intervals),
    }


def freeze_metric(ffmpeg, filters, path, timeout, args, log_path):
    settings = {
        "minimum_duration_seconds": args.freeze_duration,
        "noise_tolerance_db": args.freeze_noise_db,
    }
    if not filters.get("freezedetect"):
        return unavailable("ffmpeg filter freezedetect is unavailable", **settings)
    graph = "freezedetect=n={}dB:d={}".format(args.freeze_noise_db, args.freeze_duration)
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info", "-i", str(path),
        "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", graph, "-f", "null", "-",
    ]
    result = run_command(command, timeout)
    save_log(log_path, command, result)
    if result.get("returncode") != 0:
        return unavailable("freezedetect failed: " + result_tail(result, 2000), **settings)
    intervals, current = [], None
    for line in result_text(result).splitlines():
        start = re.search(r"lavfi\.freezedetect\.freeze_start:\s*(" + NUMBER + r")", line)
        end = re.search(r"lavfi\.freezedetect\.freeze_end:\s*(" + NUMBER + r")", line)
        duration = re.search(r"lavfi\.freezedetect\.freeze_duration:\s*(" + NUMBER + r")", line)
        if start:
            if current is not None:
                intervals.append({"start": current, "end": None, "duration": None})
            current = float(start.group(1))
        if end and current is not None:
            stop = float(end.group(1))
            intervals.append({
                "start": current, "end": stop,
                "duration": float(duration.group(1)) if duration else max(0.0, stop - current),
            })
            current = None
    if current is not None:
        intervals.append({"start": current, "end": None, "duration": None})
    known = [item["duration"] for item in intervals if item["duration"] is not None]
    return {
        "status": "ok", **settings, "intervals": intervals,
        "total_seconds": sum(known) if len(known) == len(intervals) else None,
        "event_count": len(intervals), "has_open_interval": current is not None,
    }


def blur_metric(ffmpeg, filters, path, timeout, log_path):
    if not filters.get("blurdetect"):
        return unavailable(
            "ffmpeg filter blurdetect is unavailable; no blur/pass estimate is inferred"
        )
    graph = "blurdetect"
    if filters.get("metadata"):
        graph += ",metadata=mode=print:key=lavfi.blur"
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info", "-i", str(path),
        "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", graph, "-f", "null", "-",
    ]
    result = run_command(command, timeout)
    save_log(log_path, command, result)
    if result.get("returncode") != 0:
        return unavailable("blurdetect failed: " + result_tail(result, 2000))
    text = result_text(result)
    summary = re.search(r"blur\s+mean\s*[:=]\s*(" + NUMBER + r")", text, re.IGNORECASE)
    values = [
        float(item) for item in re.findall(r"lavfi\.blur\s*[:=]\s*(" + NUMBER + r")", text)
    ]
    if summary:
        mean = float(summary.group(1))
    elif values:
        mean = statistics.fmean(values)
    else:
        return unavailable(
            "blurdetect ran but this build emitted no parseable blur summary/metadata"
        )
    answer = {
        "status": "ok", "mean": mean,
        "unit": "ffmpeg blurdetect filter-specific score; not a face/liveness verdict",
        "sample_count": len(values) if values else None,
    }
    if values:
        answer.update({"minimum": min(values), "maximum": max(values),
                       "median": statistics.median(values)})
    return answer


def add_ratios(metadata, black, freeze):
    duration = metadata.get("duration_seconds")
    for metric, name in ((black, "black"), (freeze, "freeze")):
        if metric.get("status") == "ok":
            metric["duration_ratio"] = (
                metric["total_seconds"] / duration
                if duration and duration > 0 and metric.get("total_seconds") is not None else None
            )
            metric["ratio_note"] = (
                "detected duration / container duration; threshold-dependent objective metric"
            )


def contact_sheet(ffmpeg, filters, path, metadata, output, timeout, args, log_path):
    if not filters.get("scale") or not filters.get("tile"):
        return unavailable("ffmpeg scale/tile filter is unavailable")
    samples, columns = args.samples, args.columns
    rows = (samples + columns - 1) // columns
    duration = metadata.get("duration_seconds")
    frames = metadata.get("frame_count")
    if duration and duration > 0 and filters.get("fps"):
        sampler = "fps={:.8f}".format(samples / duration)
    elif frames and frames > 0 and filters.get("select"):
        step = max(1, (frames + samples - 1) // samples)
        sampler = "select='not(mod(n,{}))'".format(step)
    else:
        return unavailable("need duration+fps filter or frame_count+select filter for sampling")
    graph = "{},scale={}:-2,tile={}x{}:padding=4:margin=4".format(
        sampler, args.sheet_width, columns, rows
    )
    command = [
        ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info", "-i", str(path),
        "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", graph,
        "-frames:v", "1", "-y", str(output),
    ]
    result = run_command(command, timeout)
    save_log(log_path, command, result)
    if result.get("returncode") != 0 or not output.is_file() or output.stat().st_size == 0:
        return unavailable("contact sheet failed: " + result_tail(result, 2000))
    return {
        "status": "ok", "path": str(output), "bytes": output.stat().st_size,
        "samples_requested": samples, "columns": columns, "rows": rows,
        "pixel_width_per_tile": args.sheet_width,
    }


def discover_videos(input_path):
    if input_path.is_file():
        return [input_path], input_path.parent
    files = sorted(
        path for path in input_path.rglob("*")
        if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES and ".state" not in path.parts
    )
    return files, input_path


def file_signature(path):
    try:
        stat = path.stat()
    except OSError as exc:
        return {"exists": False, "error": str(exc)}
    return {
        "exists": True, "device": stat.st_dev, "inode": stat.st_ino,
        "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
    }


def safe_stem(path, root, index):
    try:
        identity = str(path.relative_to(root))
    except ValueError:
        identity = str(path)
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", path.stem).strip("._") or "video"
    digest = hashlib.sha256(identity.encode("utf-8", "surrogateescape")).hexdigest()[:10]
    return "{:04d}_{}_{}".format(index, cleaned[:70], digest)


def is_within(path, directory):
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def markdown_escape(value):
    return str(value).replace("|", "\\|").replace("\n", " ")


def write_summary(report, output):
    lines = [
        "# Face video objective evidence", "",
        "Generated: {}".format(report["generated_at"]),
        "Input: {}".format(report["input"]), "",
        "This report does not determine face recognition or liveness pass/fail.",
        "Unavailable means the tool/filter/metadata was unavailable or failed; it is not a verdict.", "",
        "| File | Bytes | Codec | Resolution | Duration s | Frames | FPS | Black events | Freeze events | Blur mean | Sheet | Status |",
        "| --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for item in report["items"]:
        video = item["video"]
        black, freeze, blur = item["metrics"]["black"], item["metrics"]["freeze"], item["metrics"]["blur"]
        resolution = (
            "{}x{}".format(video.get("width"), video.get("height"))
            if video.get("width") and video.get("height") else "unavailable"
        )
        sheet = item["contact_sheet"]
        sheet_cell = (
            "[sheet]({})".format(Path(sheet["path"]).relative_to(output).as_posix())
            if sheet.get("status") == "ok" else "unavailable"
        )
        values = [
            markdown_escape(item["name"]), item["size_bytes"], video.get("codec") or "unavailable",
            resolution, video.get("duration_seconds"), video.get("frame_count"),
            video.get("frame_rate"),
            black.get("event_count", "unavailable"), freeze.get("event_count", "unavailable"),
            blur.get("mean", "unavailable"), sheet_cell, item["status"],
        ]
        lines.append("| " + " | ".join(markdown_escape(value) for value in values) + " |")
        for metric_name, metric in (("black", black), ("freeze", freeze), ("blur", blur)):
            if metric.get("status") != "ok":
                lines.append("  - {} {}: {}".format(item["name"], metric_name, metric.get("reason", "unavailable")))
    lines.extend(["", "Machine-readable details and filter settings: report.json",
                  "Raw ffmpeg metric logs: logs/"])
    (output / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Extract objective video evidence and contact sheets; never infer face/liveness pass.",
        epilog=(
            "Examples: python3 facecap_analyze.py --instance wx1 --output /tmp/wx1-face-report; "
            "python3 facecap_analyze.py /srv/redroid/wx1/local/tmp/facecap/fdv_1.mp4; "
            "python3 facecap_analyze.py /path/to/captures --no-ffprobe --sheet-format png"
        ),
    )
    parser.add_argument("input", nargs="?", help="captured video file or directory")
    parser.add_argument("--instance", help="redroid instance; reads /srv/redroid/NAME/local/tmp/facecap")
    parser.add_argument("--output", help="evidence output directory (default: a new system temp directory)")
    parser.add_argument("--ffmpeg", default=os.environ.get("FACECAP_FFMPEG", "/root/ffmpeg"),
                        help="ffmpeg executable (default: /root/ffmpeg or FACECAP_FFMPEG)")
    parser.add_argument("--ffprobe", help="ffprobe executable (default: /root/ffprobe, then PATH)")
    parser.add_argument("--no-ffprobe", action="store_true", help="exercise ffmpeg-only metadata fallback")
    parser.add_argument("--sheet-format", choices=("jpeg", "png"), default="jpeg")
    parser.add_argument("--samples", type=int, default=12, help="contact-sheet sample budget (default: 12)")
    parser.add_argument("--columns", type=int, default=4, help="contact-sheet columns (default: 4)")
    parser.add_argument("--sheet-width", type=int, default=320, help="pixel width per tile (default: 320)")
    parser.add_argument("--black-duration", type=float, default=0.10, help="blackdetect d seconds (default: 0.10)")
    parser.add_argument("--black-pixel-threshold", type=float, default=0.10,
                        help="blackdetect pix_th (default: 0.10)")
    parser.add_argument("--freeze-duration", type=float, default=0.50, help="freezedetect d seconds (default: 0.50)")
    parser.add_argument("--freeze-noise-db", type=float, default=-60.0,
                        help="freezedetect noise tolerance in dB (default: -60)")
    parser.add_argument("--timeout", type=float, default=90.0, help="timeout per media command (default: 90)")
    parser.add_argument("--strict", action="store_true", help="exit 1 if any video is partial/error")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if bool(args.input) == bool(args.instance):
        parser.error("provide exactly one positional input or --instance NAME")
    if args.instance and not re.fullmatch(r"[A-Za-z0-9_.-]+", args.instance):
        parser.error("--instance may contain only letters, digits, dot, underscore and dash")
    if args.samples < 1 or args.samples > 64 or args.columns < 1 or args.columns > 16:
        parser.error("--samples must be 1..64 and --columns 1..16")
    if args.sheet_width < 32 or args.sheet_width > 1920:
        parser.error("--sheet-width must be 32..1920")
    if args.timeout <= 0 or args.black_duration <= 0 or args.freeze_duration <= 0:
        parser.error("timeouts and detection durations must be positive")
    if not 0 <= args.black_pixel_threshold <= 1 or not -100 <= args.freeze_noise_db <= 0:
        parser.error("black pixel threshold must be 0..1; freeze noise must be -100..0 dB")

    input_path = (
        Path("/srv/redroid") / args.instance / "local" / "tmp" / "facecap"
        if args.instance else Path(args.input).expanduser()
    ).resolve()
    if not input_path.exists():
        parser.error("input does not exist: {}".format(input_path))
    try:
        ffmpeg = resolve_executable(args.ffmpeg, required=True)
        ffprobe = None if args.no_ffprobe else (
            resolve_executable(args.ffprobe, required=True) if args.ffprobe else default_ffprobe()
        )
    except CommandError as exc:
        parser.error(str(exc))

    if args.output:
        # Resolve before mkdir so a mistaken repository path creates nothing.
        output = Path(args.output).expanduser().resolve()
    else:
        output = Path(tempfile.mkdtemp(prefix="facecap-analysis-")).resolve()
    if is_within(output, REPO_ROOT):
        parser.error("refusing to save evidence inside repository: {}".format(output))
    try:
        output.mkdir(parents=True, exist_ok=True)
        output = output.resolve()
    except OSError as exc:
        parser.error("cannot create output: {}".format(exc))
    if is_within(output, REPO_ROOT):
        parser.error("refusing to save evidence inside repository: {}".format(output))
    sheets, logs = output / "sheets", output / "logs"
    sheets.mkdir(exist_ok=True)
    logs.mkdir(exist_ok=True)

    videos, root = discover_videos(input_path)
    filter_info = inspect_filters(ffmpeg, args.timeout)
    filters = filter_info["filters"]
    report = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "input": str(input_path),
        "output": str(output),
        "interpretation": "Objective media evidence only; no face-recognition/liveness pass-fail decision.",
        "tools": {
            "ffmpeg": ffmpeg, "ffprobe": ffprobe,
            "ffmpeg_version": tool_version(ffmpeg, args.timeout),
            "ffprobe_version": tool_version(ffprobe, args.timeout) if ffprobe else None,
            "filter_discovery": filter_info,
        },
        "items": [],
    }
    if not videos:
        report["warning"] = "no supported video files found"

    for index, path in enumerate(videos, 1):
        stem = safe_stem(path, root, index)
        suffix = ".jpg" if args.sheet_format == "jpeg" else ".png"
        before = file_signature(path)
        item = {
            "name": path.name, "path": str(path),
            "size_bytes": before.get("size", 0),
            "status": "pending", "video": {}, "metrics": {}, "contact_sheet": {},
            "analysis_warnings": [],
        }
        report["items"].append(item)
        try:
            if not before.get("exists"):
                raise FileNotFoundError("source disappeared before analysis")
            if item["size_bytes"] == 0:
                item["video"] = {"status": "error", "reason": "empty file"}
                item["metrics"] = {
                    "black": unavailable("empty file"), "freeze": unavailable("empty file"),
                    "blur": unavailable("empty file"),
                }
                item["contact_sheet"] = unavailable("empty file")
                item["status"] = "error"
                continue

            metadata = get_metadata(ffprobe, ffmpeg, path, args.timeout)
            if metadata.get("frame_count") is None and metadata.get("status") in ("ok", "degraded"):
                counted, command, result = count_decoded_frames(ffmpeg, path, args.timeout)
                save_log(logs / (stem + ".framecount.log"), command, result)
                if counted.get("status") == "ok":
                    metadata["frame_count"] = counted["frame_count"]
                    metadata["frame_count_source"] = counted["frame_count_source"]
                else:
                    metadata["frame_count_unavailable"] = counted["reason"]
            if metadata.get("frame_rate") is None:
                frames, duration = metadata.get("frame_count"), metadata.get("duration_seconds")
                if frames is not None and duration and duration > 0:
                    metadata["frame_rate"] = frames / duration
                    metadata["frame_rate_source"] = "decoded_frames/duration"
            item["video"] = metadata

            if metadata.get("status") not in ("ok", "degraded"):
                item["metrics"] = {
                    "black": unavailable("video metadata/decode unavailable"),
                    "freeze": unavailable("video metadata/decode unavailable"),
                    "blur": unavailable("video metadata/decode unavailable"),
                }
                item["contact_sheet"] = unavailable("video metadata/decode unavailable")
                item["status"] = "error"
                continue

            black = black_metric(ffmpeg, filters, path, args.timeout, args, logs / (stem + ".black.log"))
            freeze = freeze_metric(ffmpeg, filters, path, args.timeout, args, logs / (stem + ".freeze.log"))
            blur = blur_metric(ffmpeg, filters, path, args.timeout, logs / (stem + ".blur.log"))
            add_ratios(metadata, black, freeze)
            item["metrics"] = {"black": black, "freeze": freeze, "blur": blur}
            item["contact_sheet"] = contact_sheet(
                ffmpeg, filters, path, metadata, sheets / (stem + suffix), args.timeout, args,
                logs / (stem + ".sheet.log"),
            )
            item["status"] = "ok"
        except OSError as exc:
            item["status"] = "error"
            item["analysis_error"] = "source/output I/O failure: {}".format(exc)
            if not item["video"]:
                item["video"] = {"status": "error", "reason": str(exc)}
            if not item["metrics"]:
                item["metrics"] = {
                    "black": unavailable("source I/O failure"),
                    "freeze": unavailable("source I/O failure"),
                    "blur": unavailable("source I/O failure"),
                }
            if not item["contact_sheet"]:
                item["contact_sheet"] = unavailable("source I/O failure")
        finally:
            after = file_signature(path)
            identity_keys = ("device", "inode", "size", "mtime_ns")
            stable = bool(before.get("exists") and after.get("exists") and
                          all(before.get(key) == after.get(key) for key in identity_keys))
            item["source_stability"] = {
                "stable_during_analysis": stable, "before": before, "after": after,
            }
            if not stable:
                warning = "source changed/disappeared during analysis; rerun on a stable capture"
                item["analysis_warnings"].append(warning)
                item["source_stability"]["warning"] = warning
                if item["status"] == "ok":
                    item["status"] = "partial"

    report["summary"] = {
        "videos": len(report["items"]),
        "ok": sum(item["status"] == "ok" for item in report["items"]),
        "partial": sum(item["status"] == "partial" for item in report["items"]),
        "error": sum(item["status"] == "error" for item in report["items"]),
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    write_summary(report, output)
    print("Evidence output: {}".format(output))
    print("Videos: {videos}; ok={ok}; partial={partial}; error={error}".format(**report["summary"]))
    if not ffprobe:
        print("ffprobe: unavailable (ffmpeg fallback used; see report.json)")
    print("No face/liveness pass-fail conclusion is made.")
    if args.strict and any(item["status"] != "ok" for item in report["items"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
