"""A fake yt-dlp for tests: understands the options the ytdlp add-on passes.

URLs look like fake://<extractor>/<id>[,<id>...]?query. Query keys:
  uploader, tags (comma separated), categories (comma separated), title
  slow=1    after finishing a file, wait (up to 10 s) until it was moved away by the add-on
  fail=1    exit 1 after handling this URL (like a broken item in a playlist)
  sleep=N   sleep N seconds before starting
Every downloaded file is written as <work>/<ExtractorKey>/<id>.mp4 with an .info.json next to
it, the archive gets "<extractor> <id>", and the --print-to-file template is appended to its
file with %(field)s replaced, like the real tool at the after_move stage.
"""

import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def expand(template: str, info: dict) -> str:
    out = re.sub(r"%\((\w+)\)s", lambda m: str(info.get(m.group(1), "NA")), template)
    return out.replace("%%", "%")


def main(argv: list[str]) -> int:
    print("[debug] fake yt-dlp", " ".join(argv))
    opts: dict[str, object] = {"print_to_file": []}
    urls: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg == "--":
            urls.extend(it)
            break
        if arg in ("-o", "-f", "--download-archive", "--cookies", "--dateafter", "--max-downloads"):
            opts[arg] = next(it)
        elif arg == "--print-to-file":
            opts["print_to_file"].append((next(it), next(it)))
        elif arg.startswith("-"):
            opts.setdefault("flags", []).append(arg)
        else:
            urls.append(arg)
    archive = Path(opts["--download-archive"]) if "--download-archive" in opts else None
    recorded = set(archive.read_text().splitlines()) if archive and archive.exists() else set()
    max_downloads = int(opts.get("--max-downloads", 0)) or None
    downloads = 0
    exit_code = 0
    for url in urls:
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        time.sleep(float(query.get("sleep", 0)))
        extractor = parts.netloc
        key = extractor.capitalize()
        for video_id in [v for v in parts.path.strip("/").split(",") if v]:
            line = f"{extractor} {video_id}"
            if line in recorded:
                print(f"[download] {video_id} has already been recorded in the archive")
                continue
            if max_downloads and downloads >= max_downloads:
                print("[info] Maximum number of downloads reached, stopping due to --max-downloads")
                return 101
            info = {
                "id": video_id,
                "title": query.get("title", f"Video {video_id}"),
                "description": f"About {video_id}",
                "uploader": query.get("uploader"),
                "upload_date": "20260102",
                "duration": 12,
                "extractor": extractor,
                "extractor_key": key,
                "webpage_url": f"https://{extractor}.example/watch/{video_id}",
                "tags": [t for t in query.get("tags", "").split(",") if t],
                "categories": [c for c in query.get("categories", "").split(",") if c],
                "ext": "mp4",
            }
            media = Path(expand(opts["-o"], info))
            media.parent.mkdir(parents=True, exist_ok=True)
            infojson = media.with_suffix(".info.json")
            infojson.write_text(json.dumps(info))
            media.write_bytes(f"FAKE VIDEO {extractor} {video_id}\n".encode() * 64)
            print(f"[download] Destination: {media}")
            info["filepath"] = str(media)
            info["infojson_filename"] = str(infojson)
            for spec, file_template in opts["print_to_file"]:
                when, _, template = spec.partition(":")
                assert when == "after_move", spec
                with open(expand(file_template, info), "a", encoding="utf-8") as fh:
                    fh.write(expand(template, info) + "\n")
            if archive:
                with open(archive, "a") as fh:
                    fh.write(line + "\n")
            downloads += 1
            if query.get("slow"):
                deadline = time.monotonic() + 10
                while media.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                if media.exists():
                    print("ERROR: slow mode: the file was not taken while yt-dlp was running")
                    return 3
                print(f"[fake] {video_id} was taken while running")
        if query.get("fail"):
            print(f"ERROR: [{extractor}] broken: Unable to download webpage")
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
