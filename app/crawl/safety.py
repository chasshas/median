"""Validation of user-supplied command-line options for crawler tools (extra_args).

Each tool gets a denylist of options that run commands, change where files are written, load
other configuration, code or URL lists, or evaluate Python. Both tools accept unambiguous
abbreviations of long options ("--exec-b" is --exec-before-download, "--batch-fil" is
--batch-file) and clusters of short options ("-ixo FILE" is -i -x -o FILE), so the check is:

- a long option ("--name" or "--name=value") is refused when its name, compared without case,
  is a denied option or a prefix of one, unless it is exactly one of the tool's own options
  that happen to be such a prefix (yt-dlp's --print and --netrc);
- a short option cluster ("-abc") is refused when it contains a denied letter before the
  first letter that takes a value (the rest of the cluster is that value).

Every element of extra_args is checked, values included: a value that starts with "-" must be
passed as "--option=value". Elements that do not start with "-" are values or URLs.
"""

from collections.abc import Iterable
from dataclasses import dataclass


class UnsafeArgument(ValueError):
    pass


@dataclass(frozen=True)
class OptionPolicy:
    tool: str
    denied_long: frozenset[str]
    denied_short: frozenset[str]
    value_short: frozenset[str]  # short options that take a value (end of a cluster)
    allowed_exact: frozenset[str] = frozenset()

    def check(self, args: Iterable[str]) -> list[str]:
        checked = []
        for arg in args:
            if not isinstance(arg, str):
                raise UnsafeArgument(f"extra argument {arg!r} is not a string")
            if "\x00" in arg:
                raise UnsafeArgument("extra arguments must not contain NUL characters")
            reason = self._refusal(arg)
            if reason:
                raise UnsafeArgument(f"{self.tool} option {arg!r} is not allowed: {reason}")
            checked.append(arg)
        return checked

    def _refusal(self, arg: str) -> str | None:
        if arg.startswith("--"):
            name = arg.split("=", 1)[0]
            if name == "--":
                return None
            if name in self.allowed_exact:
                return None
            folded = name.casefold()
            for denied in sorted(self.denied_long):
                if denied.casefold().startswith(folded):
                    return f"it is (or abbreviates) {denied}"
            return None
        if arg.startswith("-") and len(arg) > 1:
            for letter in arg[1:]:
                if letter in self.denied_short:
                    return f"it contains -{letter}"
                if letter in self.value_short:
                    break
        return None


# Checked against yt-dlp 2026.8.19 (yt_dlp/options.py).
YTDLP_POLICY = OptionPolicy(
    tool="yt-dlp",
    denied_long=frozenset(
        {
            # run commands or programs
            "--exec",
            "--exec-before-download",
            "--netrc-cmd",
            "--downloader",
            "--external-downloader",
            "--downloader-args",
            "--external-downloader-args",
            "--postprocessor-args",
            "--ppa",
            "--use-postprocessor",
            "--ffmpeg-location",
            "--js-runtimes",
            "--plugin-dirs",
            "--alias",
            # output locations and extra files
            "--output",
            "--paths",
            "--print-to-file",
            "--write-link",
            "--write-url-link",
            "--write-webloc-link",
            "--write-desktop-link",
            "--cache-dir",
            "--download-archive",
            # other configuration, input lists and credentials files
            "--config-locations",
            "--batch-file",
            "--load-info-json",
            "--cookies",
            "--netrc-location",
            # self update of the tool venv
            "--update",
            "--update-to",
        }
    ),
    denied_short=frozenset("oPaU"),
    value_short=frozenset("2CINOPRSafoprtu"),
    allowed_exact=frozenset({"--print", "--netrc"}),
)

# Checked against gallery-dl 1.32.17 (gallery_dl/option.py). Filters and format strings
# ("\fE ...") are evaluated as Python, so every option taking an expression or a format is out.
GALLERYDL_POLICY = OptionPolicy(
    tool="gallery-dl",
    denied_long=frozenset(
        {
            # run commands, load code, evaluate Python
            "--exec",
            "--exec-after",
            "--extractors",
            "--postprocessor",
            "--postprocessor-option",
            "--filter",
            "--post-filter",
            "--child-filter",
            "--file-filter",
            "--image-filter",
            "--chapter-filter",
            "--print",
            "--Print",
            "--server",
            "--option",
            "--config-open",
            # output locations and extra files
            "--destination",
            "--directory",
            "--filename",
            "--rename",
            "--rename-to",
            "--mtime",
            "--print-to-file",
            "--Print-to-file",
            "--error-file",
            "--write-log",
            "--write-unsupported",
            "--cookies-export",
            "--cache-file",
            "--download-archive",
            # other configuration, input lists and credentials files
            "--config",
            "--config-json",
            "--config-yaml",
            "--config-toml",
            "--config-create",
            "--input-file",
            "--input-file-comment",
            "--input-file-delete",
            "--cookies",
            "--update-check",
        }
    ),
    denied_short=frozenset("dDfXSUiIxeNocCPO"),
    value_short=frozenset("ACDINOPRTXacdefioprux"),
)


def check_ytdlp_args(args: list[str]) -> list[str]:
    return YTDLP_POLICY.check(args)


def check_gallerydl_args(args: list[str]) -> list[str]:
    return GALLERYDL_POLICY.check(args)


def check_urls(urls: list[str]) -> list[str]:
    """URLs are passed after "--"; refuse empty ones and ones that look like options anyway."""
    cleaned = [u.strip() for u in urls]
    if not cleaned or any(not u for u in cleaned):
        raise ValueError("give at least one URL, one per line")
    for url in cleaned:
        if url.startswith("-"):
            raise ValueError(f"{url!r} is not a URL")
    return cleaned
