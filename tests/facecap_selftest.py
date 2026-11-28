#!/usr/bin/env python3
"""Standalone tests for facecap_watch.sh and facecap_analyze.py.

No WeChat data is read. All fixtures/reports live in a temporary directory.
With ffmpeg, real synthetic videos are generated. Without ffmpeg, a clearly
labelled mock executable tests the analyzer contract instead of media decoding.
"""

import argparse
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
JPEG_BYTES = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
    "2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/"
    "8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAF//8QA"
    "FBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QA"
    "FBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QA"
    "FBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABB//8QAFBEBAAAAAAAAAAAAAAAAAAAA"
    "AP/aAAgBAwEBPxB//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPxB//8QAFBABAAAAAAAAAAAAAAAAAAAA"
    "AP/aAAgBAQABPxB//9k="
)

MOCK_MEDIA = r'''#!/usr/bin/env python3
import base64
import json
import os
from pathlib import Path
import sys

mode = sys.argv[1]
args = sys.argv[2:]
png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
jpeg = base64.b64decode("/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////2wBDAf//////////////////////////////////////////////////////////////////////////////////////wAARCAABAAEDASIAAhEBAxEB/8QAFQABAQAAAAAAAAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/9oADAMBAAIQAxAAAAF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABBQJ//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPwF//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPwF//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQAGPwJ//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPyF//9oADAMBAAIAAwAAABB//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAwEBPxB//8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAgBAgEBPxB//8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxB//9k=")
if "-version" in args:
    print(mode + " version mock-facecap-1")
    raise SystemExit(0)
if mode == "ffprobe":
    print(json.dumps({
        "streams": [{
            "index": 0, "codec_type": "video", "codec_name": "mpeg4",
            "codec_long_name": "Mock MPEG-4", "profile": "Simple Profile",
            "width": 96, "height": 64, "pix_fmt": "yuv420p",
            "r_frame_rate": "12/1", "avg_frame_rate": "12/1",
            "nb_frames": "24", "nb_read_frames": "24", "duration": "2.0"
        }],
        "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "2.0", "size": "1234"}
    }))
    raise SystemExit(0)
if "-filters" in args:
    print("Filters:\n  T.. = Timeline support")
    if not os.environ.get("MOCK_NO_FILTERS"):
        for name in ("blackdetect", "freezedetect", "blurdetect", "metadata", "fps", "select", "scale", "tile"):
            print("  ... {:16s} V->V       mock filter".format(name))
    raise SystemExit(0)
input_name = args[args.index("-i") + 1] if "-i" in args else "mock.mp4"
# Metadata-only invocation has no output format and intentionally exits 1 like ffmpeg -i.
if "-f" not in args and "-vf" not in args:
    sys.stderr.write(
        "Duration: 00:00:02.00, start: 0.000000, bitrate: 100 kb/s\n"
        "  Stream #0:0: Video: mpeg4 (Simple Profile), yuv420p, 96x64 [SAR 1:1 DAR 3:2], 12 fps, 12 tbr, 12288 tbn\n"
    )
    raise SystemExit(1)
if "-progress" in args:
    print("frame=24")
    print("progress=end")
    raise SystemExit(0)
graph = args[args.index("-vf") + 1] if "-vf" in args else ""
if "blackdetect" in graph:
    sys.stderr.write("[blackdetect @ 0x1] black_start:0.25 black_end:0.75 black_duration:0.5\n")
if "freezedetect" in graph:
    sys.stderr.write("[freezedetect @ 0x1] lavfi.freezedetect.freeze_start: 0.5\n")
    sys.stderr.write("[freezedetect @ 0x1] lavfi.freezedetect.freeze_end: 1.25 lavfi.freezedetect.freeze_duration: 0.75\n")
if "blurdetect" in graph:
    print("frame:0 pts:0 pts_time:0 lavfi.blur=2.0")
    print("frame:1 pts:1 pts_time:0.0833 lavfi.blur=4.0")
    sys.stderr.write("[blurdetect @ 0x1] blur mean: 3.0\n")
if "tile=" in graph:
    output = Path(args[-1])
    output.write_bytes(png if output.suffix.lower() == ".png" else jpeg)
raise SystemExit(0)
'''


class Failure(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Failure(message)


def run(command, timeout=30, env=None):
    completed = subprocess.run(
        [str(item) for item in command], stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace", timeout=timeout, env=env,
    )
    return completed


def bash_path(path):
    text = str(path)
    if os.name == "nt" and len(text) >= 3 and text[1:3] in (":\\", ":/"):
        return "/" + text[0].lower() + "/" + text[2:].replace("\\", "/").lstrip("/")
    return text


def wait_until(predicate, timeout=3.0, description="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise Failure("timed out waiting for " + description)


def log_rows(output):
    log = output / "capture_log.tsv"
    if not log.exists():
        return []
    lines = log.read_text(encoding="utf-8").splitlines()
    return [dict(zip(lines[0].split("\t"), line.split("\t"))) for line in lines[1:]]


def test_watcher(context):
    shell = context["shell"]
    if not shell:
        return "SKIP: no POSIX sh/bash found"
    source, output = context["temp"] / "source", context["temp"] / "capture"
    source.mkdir()
    command = [
        shell, bash_path(context["watcher"]), "--source", bash_path(source),
        "--output", bash_path(output), "--interval", "0.05",
        "--stable-polls", "5", "--duration", "4",
    ]
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        wait_until(lambda: output.joinpath("capture_log.tsv").exists(), description="watcher start")
        growing = source / "fdv_synthetic.mp4"
        growing.write_bytes(b"first-")
        time.sleep(0.13)
        with growing.open("ab") as handle:
            handle.write(b"final")
        (source / "thumb_synthetic.jpg").write_bytes(b"thumbnail-original-bytes")
        (source / "fdv_empty.mp4").write_bytes(b"")

        deleted = source / "fdv_deleted.mp4"
        deleted.write_bytes(b"deleted-after-snapshot")
        wait_until(lambda: (output / deleted.name).exists(), description="early deleted-file snapshot")
        deleted.unlink()

        duplicate = run(command[:-1] + ["1"], timeout=5)
        require(duplicate.returncode == 2,
                "duplicate watcher should exit 2, got {}: {}".format(duplicate.returncode, duplicate.stderr))
        require("already running" in duplicate.stderr, "duplicate watcher reason missing")

        wait_until(lambda: any(row.get("event") == "stable" and row.get("source_name") == growing.name
                               for row in log_rows(output)), description="stable growing capture")
        stdout, stderr = process.communicate(timeout=8)
        require(process.returncode == 0, "watcher failed: {} {}".format(stdout, stderr))
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=5)

    require((output / growing.name).read_bytes() == b"first-final", "growing capture bytes differ")
    require((output / "thumb_synthetic.jpg").read_bytes() == b"thumbnail-original-bytes", "thumb bytes differ")
    require((output / deleted.name).read_bytes() == b"deleted-after-snapshot", "deleted capture was not retained")
    require((output / "fdv_empty.mp4").read_bytes() == b"", "empty capture was not retained")
    require(not (output / ".watcher.lock").exists(), "watcher lock was not cleaned")
    rows = log_rows(output)
    require(all(row.get("timestamp") for row in rows), "timestamp missing from capture log")
    stable_rows = [row for row in rows if row.get("event") == "stable" and row.get("source_name") == growing.name]
    require(len(stable_rows) == 1, "stable file was copied/logged as stable more than once")
    removed = [row for row in rows if row.get("event") == "source_removed" and row.get("source_name") == deleted.name]
    require(len(removed) == 1 and removed[0].get("stable") == "0", "early unlink was not marked unstable exactly once")
    require(any(row.get("event") == "stable" and row.get("source_name") == "fdv_empty.mp4" for row in rows),
            "empty file was not handled without crashing")
    return "PASS: growing/empty/thumb/unlink bytes, timestamps, stable dedupe, duplicate lock"


def make_mock(context):
    script = context["temp"] / "mock_media.py"
    script.write_text(MOCK_MEDIA, encoding="utf-8")
    executables = {}
    for mode in ("ffmpeg", "ffprobe"):
        if os.name == "nt":
            wrapper = context["temp"] / ("mock-" + mode + ".cmd")
            wrapper.write_text(
                '@echo off\r\n"{}" "{}" {} %*\r\n'.format(sys.executable, script, mode),
                encoding="utf-8",
            )
        else:
            wrapper = context["temp"] / ("mock-" + mode + ".sh")
            wrapper.write_text(
                '#!/bin/sh\nexec "{}" "{}" {} "$@"\n'.format(sys.executable, script, mode),
                encoding="utf-8",
            )
            wrapper.chmod(0o700)
        executables[mode] = wrapper
    return executables["ffmpeg"], executables["ffprobe"]


def real_executable(value, fallback):
    if value:
        path = Path(value)
        return str(path) if path.is_file() else shutil.which(value)
    fixed = Path(fallback)
    if fixed.is_file():
        return str(fixed)
    return shutil.which(fixed.name)


def make_real_videos(context, ffmpeg):
    media = context["temp"] / "media"
    media.mkdir()
    commands = [
        ("fdv_synthetic.mp4", "testsrc2=size=96x64:rate=12:duration=2"),
        ("fdv_black.mp4", "color=c=black:size=96x64:rate=12:duration=1.2"),
        ("fdv_freeze.mp4", "color=c=red:size=96x64:rate=12:duration=1.2"),
    ]
    for name, source in commands:
        result = run([
            ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", source, "-c:v", "mpeg4", "-q:v", "5",
            "-pix_fmt", "yuv420p", "-y", media / name,
        ], timeout=45)
        require(result.returncode == 0, "synthetic ffmpeg generation failed: " + result.stderr)
    (media / "fdv_empty.mp4").write_bytes(b"")
    return media


def run_analyzer(context, source, output, ffmpeg, ffprobe, mock_env=None):
    command = [
        sys.executable, context["analyzer"], source, "--output", output,
        "--ffmpeg", ffmpeg, "--sheet-format", "png", "--samples", "4",
        "--columns", "2", "--timeout", "30",
    ]
    if ffprobe:
        command.extend(["--ffprobe", ffprobe])
    else:
        command.append("--no-ffprobe")
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if mock_env:
        env.update(mock_env)
    result = run(command, timeout=150, env=env)
    require(result.returncode == 0, "analyzer failed: {}\n{}".format(result.stdout, result.stderr))
    require((output / "report.json").is_file(), "report.json missing")
    require((output / "summary.md").is_file(), "summary.md missing")
    return json.loads((output / "report.json").read_text(encoding="utf-8"))


def check_report(context, report, output, mocked):
    require(report["summary"]["videos"] == 4, "expected three synthetic videos plus one empty file")
    by_name = {item["name"]: item for item in report["items"]}
    normal = by_name["fdv_synthetic.mp4"]
    require(normal["size_bytes"] > 0, "file size missing")
    require(normal["video"]["codec"] == "mpeg4", "codec missing")
    require((normal["video"]["width"], normal["video"]["height"]) == (96, 64), "dimensions differ")
    require(abs(normal["video"]["duration_seconds"] - 2.0) < 0.15, "duration differs")
    require(normal["video"]["frame_count"] >= 12, "decoded frame count missing")
    require(abs(normal["video"]["frame_rate"] - 12.0) < 0.2, "frame rate differs")
    sheet = normal["contact_sheet"]
    require(sheet["status"] == "ok", "contact sheet unavailable: " + str(sheet))
    sheet_path = Path(sheet["path"])
    require(sheet_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"), "contact sheet is not PNG")
    require(by_name["fdv_empty.mp4"]["status"] == "error", "empty file was not an explicit error")
    require(by_name["fdv_empty.mp4"]["video"]["reason"] == "empty file", "empty reason missing")
    for name, metric, minimum in (("fdv_black.mp4", "black", 1), ("fdv_freeze.mp4", "freeze", 1)):
        value = by_name[name]["metrics"][metric]
        if value["status"] == "ok":
            require(value["event_count"] >= minimum, "{} did not detect synthetic {}".format(metric, name))
        else:
            require(value["status"] == "unavailable" and value.get("reason"),
                    "missing filter was not explained")
    blur = normal["metrics"]["blur"]
    require(blur["status"] in ("ok", "unavailable"), "unexpected blur state")
    if blur["status"] == "ok":
        require(isinstance(blur["mean"], (float, int)), "blur mean missing")
    else:
        require(blur.get("reason"), "blur unavailable reason missing")
    if mocked:
        require(normal["metrics"]["black"]["event_count"] == 1, "mock black parser failed")
        require(normal["metrics"]["freeze"]["event_count"] == 1, "mock freeze parser failed")
        require(blur["mean"] == 3.0, "mock blur parser failed")
    return by_name


def test_analyzer(context):
    engine = context["engine"]
    if engine == "real":
        ffmpeg, ffprobe = context["ffmpeg"], context["ffprobe"]
        media = make_real_videos(context, ffmpeg)
        mocked = False
    else:
        ffmpeg, ffprobe = make_mock(context)
        media = context["temp"] / "media"
        media.mkdir()
        for name in ("fdv_synthetic.mp4", "fdv_black.mp4", "fdv_freeze.mp4"):
            (media / name).write_bytes(b"mock synthetic media fixture")
        (media / "fdv_empty.mp4").write_bytes(b"")
        mocked = True
    output = context["temp"] / "analysis"
    report = run_analyzer(context, media, output, ffmpeg, ffprobe)
    check_report(context, report, output, mocked)

    # Explicitly exercise no-ffprobe metadata and decoded-frame fallback.
    fallback_output = context["temp"] / "analysis-no-ffprobe"
    fallback = run_analyzer(
        context, media / "fdv_synthetic.mp4", fallback_output, ffmpeg, None,
    )
    item = fallback["items"][0]
    require(item["video"]["status"] == "degraded", "ffmpeg-only fallback was not marked degraded")
    require(item["video"]["width"] == 96 and item["video"]["height"] == 64, "fallback dimensions missing")
    require(item["video"]["frame_count"] >= 12, "fallback decoded frame count missing")

    if mocked:
        # Simulate a build without filters: all three metrics and sheet must say unavailable.
        no_filter_output = context["temp"] / "analysis-no-filters"
        no_filters = run_analyzer(
            context, media / "fdv_synthetic.mp4", no_filter_output, ffmpeg, ffprobe,
            mock_env={"MOCK_NO_FILTERS": "1"},
        )
        item = no_filters["items"][0]
        for metric in ("black", "freeze", "blur"):
            require(item["metrics"][metric]["status"] == "unavailable", metric + " was guessed")
            require(item["metrics"][metric].get("reason"), metric + " unavailable reason missing")
        require(item["contact_sheet"]["status"] == "unavailable", "sheet should be unavailable without filters")
        serialized = json.dumps(no_filters).lower()
        require('"face_passed"' not in serialized and '"liveness_passed"' not in serialized,
                "selftest found a face/liveness verdict")
    return ("PASS: mock analyzer contract, parser, empty input, fallbacks, unavailable filters"
            if mocked else "PASS: real synthetic video metadata, sheets, black/freeze/blur and fallbacks")


def build_parser():
    parser = argparse.ArgumentParser(
        description="Self-test face capture tools using only temporary synthetic/mock fixtures."
    )
    parser.add_argument("--analyzer", default=str(HERE / "facecap_analyze.py"))
    parser.add_argument("--watcher", default=str(HERE / "facecap_watch.sh"))
    parser.add_argument("--shell", help="POSIX sh/bash for watcher test (default: bash, sh, then PATH)")
    parser.add_argument("--media-engine", choices=("auto", "real", "mock"), default="auto",
                        help="real creates synthetic videos; auto falls back to an explicitly labelled mock")
    parser.add_argument("--ffmpeg", help="real ffmpeg path for --media-engine auto/real")
    parser.add_argument("--ffprobe", help="real ffprobe path (optional)")
    parser.add_argument("--keep-temp", action="store_true", help="keep temporary fixtures/reports after success")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    analyzer, watcher = Path(args.analyzer).resolve(), Path(args.watcher).resolve()
    if not analyzer.is_file() or not watcher.is_file():
        raise SystemExit("selftest: analyzer or watcher not found")
    shell = args.shell or shutil.which("bash") or shutil.which("sh")
    ffmpeg = real_executable(args.ffmpeg, "/root/ffmpeg")
    ffprobe = real_executable(args.ffprobe, "/root/ffprobe")
    if args.media_engine == "real" and not ffmpeg:
        raise SystemExit("selftest: real ffmpeg requested but not found")
    engine = "real" if args.media_engine == "real" or (args.media_engine == "auto" and ffmpeg) else "mock"
    temp = Path(tempfile.mkdtemp(prefix="facecap-selftest-"))
    context = {
        "analyzer": analyzer, "watcher": watcher, "shell": shell,
        "ffmpeg": ffmpeg, "ffprobe": ffprobe, "engine": engine, "temp": temp,
    }
    print("Selftest temp: {}".format(temp))
    print("Media engine: {}{}".format(engine, " (ffmpeg not found; protocol mock, not a decode test)" if engine == "mock" else ""))
    results = []
    failed = False
    for name, function in (("watcher", test_watcher), ("analyzer", test_analyzer)):
        started = time.monotonic()
        try:
            detail = function(context)
            results.append({"name": name, "result": detail, "seconds": round(time.monotonic() - started, 3)})
            print("{}: {}".format(name, detail))
        except Exception as exc:
            failed = True
            detail = "FAIL: {}: {}".format(type(exc).__name__, exc)
            results.append({"name": name, "result": detail, "seconds": round(time.monotonic() - started, 3)})
            print(detail, file=sys.stderr)
    print(json.dumps({"media_engine": engine, "results": results}, ensure_ascii=False, indent=2))
    if failed:
        print("RESULT FAIL; fixtures retained at {}".format(temp), file=sys.stderr)
        return 1
    print("RESULT PASS")
    if args.keep_temp:
        print("Fixtures retained at {}".format(temp))
    else:
        shutil.rmtree(temp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
