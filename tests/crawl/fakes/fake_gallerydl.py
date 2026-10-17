"""A fake gallery-dl for tests: understands the options the gallerydl add-on passes.

URLs look like fake://<category>/<post id>?query. Query keys:
  files=N       number of files in the post (default 1)
  noid=1        no "id" in the metadata (remote id falls back to the filename)
  tags=...      "tags" field as a list (comma separated); tagstr=... as one space separated
                string; artist=...; author=... (nested as author.name)
  grow=1        write the sidecar, then the file under its final name in pieces 0.4 s apart
  partial=1     leave a .part file with a sidecar that never completes
  nosidecar=1   write the file without a sidecar
  slow=1        after each file, wait (up to 10 s) until the add-on moved it away
  fail=1        exit 4 after this URL
Files go to <-D>/<category>_<id>_<num>.png with "<file>.json" sidecars written before the
.part file is renamed, like the real tool.
"""

import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def main(argv: list[str]) -> int:
    print("fake gallery-dl", " ".join(argv))
    opts: dict[str, str] = {}
    urls: list[str] = []
    it = iter(argv)
    for arg in it:
        if arg == "--":
            urls.extend(it)
            break
        if arg in ("-D", "--download-archive", "--cookies", "--range"):
            opts[arg] = next(it)
        elif arg.startswith("-"):
            continue
        else:
            urls.append(arg)
    target = Path(opts["-D"])
    target.mkdir(parents=True, exist_ok=True)
    archive = None
    if "--download-archive" in opts:
        archive = sqlite3.connect(opts["--download-archive"])
        archive.execute("CREATE TABLE IF NOT EXISTS archive (entry TEXT PRIMARY KEY)")
    exit_code = 0
    for url in urls:
        parts = urlsplit(url)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        category = parts.netloc
        post = parts.path.strip("/")
        for num in range(1, int(query.get("files", 1)) + 1):
            name = f"{category}_{post}_{num}"
            data: dict = {
                "category": category,
                "subcategory": "post",
                "filename": name,
                "extension": "png",
                "num": num,
                "title": f"Post {post}",
            }
            if not query.get("noid"):
                data["id"] = post
            if "tags" in query:
                data["tags"] = [t for t in query["tags"].split(",") if t]
            if "tagstr" in query:
                data["tag_string"] = query["tagstr"]
            if "artist" in query:
                data["artist"] = query["artist"]
            if "author" in query:
                data["author"] = {"name": query["author"], "id": 7}
            final = target / f"{name}.png"
            sidecar = target / f"{name}.png.json"
            content = f"FAKE IMAGE {category} {post} {num}\n".encode() * 200
            if query.get("partial"):
                (target / f"{name}.png.part").write_bytes(content[:100])
                sidecar.write_text(json.dumps(data))
                continue
            if query.get("grow"):
                # Sidecar first, then the file grows in place: only the size check can tell.
                sidecar.write_text(json.dumps(data))
                with open(final, "wb") as fh:
                    for i in range(0, len(content), len(content) // 4):
                        fh.write(content[i : i + len(content) // 4])
                        fh.flush()
                        time.sleep(0.4)
                if not final.exists():
                    print("ERROR: grow mode: the file was taken before it was complete")
                    return 3
            else:
                part = target / f"{name}.png.part"
                part.write_bytes(content)
                if not query.get("nosidecar"):
                    sidecar.write_text(json.dumps(data))
                part.rename(final)
            print(final)
            if archive is not None:
                archive.execute(
                    "INSERT OR IGNORE INTO archive VALUES (?)", (f"{category}{post}_{num}",)
                )
                archive.commit()
            if query.get("slow"):
                deadline = time.monotonic() + 10
                while final.exists() and time.monotonic() < deadline:
                    time.sleep(0.05)
                if final.exists():
                    print("ERROR: slow mode: the file was not taken while gallery-dl was running")
                    return 3
                print(f"[fake] {name} was taken while running")
        if query.get("fail"):
            print(f"[{category}][error] HttpError: 404 Not Found")
            exit_code = 4
    return exit_code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
