"""Media for the phase 6 smoke checks: python scripts/smoke_crawl_media.py <dir> <tag>.

<dir>/clip-<tag>.mp4 (for yt-dlp's generic extractor), <dir>/gallery-<tag>-{1,2}.png (for
gallery-dl's directlink extractor) and <dir>/direct-<tag>-{1,2}.png (for the direct add-on).
"""

import sys
from pathlib import Path

from smoke_media import image, video


def main() -> None:
    root, tag = Path(sys.argv[1]), sys.argv[2]
    root.mkdir(parents=True, exist_ok=True)
    video(root / f"clip-{tag}.mp4", seconds=2.0)
    for n in (1, 2):
        image(root / f"gallery-{tag}-{n}.png")
        image(root / f"direct-{tag}-{n}.png")


if __name__ == "__main__":
    main()
