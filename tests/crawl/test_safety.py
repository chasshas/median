import pytest

from app.crawl.safety import (
    GALLERYDL_POLICY,
    YTDLP_POLICY,
    UnsafeArgument,
    check_gallerydl_args,
    check_urls,
    check_ytdlp_args,
)

YTDLP_REQUIRED = [
    "--exec",
    "--exec-before-download",
    "-o",
    "--output",
    "-P",
    "--paths",
    "--config-locations",
    "-a",
    "--batch-file",
    "--print-to-file",
    "--write-link",
    "--write-url-link",
    "--write-webloc-link",
    "--write-desktop-link",
    "--load-info-json",
]
GALLERYDL_REQUIRED = [
    "--exec",
    "--exec-after",
    "-d",
    "--destination",
    "-D",
    "--directory",
    "-c",
    "--config",
    "--config-json",
    "--config-yaml",
    "--config-toml",
    "-i",
    "--input-file",
    "--download-archive",
    "--postprocessor",
    "-P",
]


def forms(option: str) -> list[list[str]]:
    """The option alone, with a separate value, and in its "=value" form."""
    return [[option], [option, "x"], [f"{option}=x"]]


@pytest.mark.parametrize("option", YTDLP_REQUIRED + sorted(YTDLP_POLICY.denied_long))
def test_ytdlp_denied_options(option: str) -> None:
    for args in forms(option):
        with pytest.raises(UnsafeArgument):
            check_ytdlp_args(args)


@pytest.mark.parametrize("option", GALLERYDL_REQUIRED + sorted(GALLERYDL_POLICY.denied_long))
def test_gallerydl_denied_options(option: str) -> None:
    for args in forms(option):
        with pytest.raises(UnsafeArgument):
            check_gallerydl_args(args)


@pytest.mark.parametrize(
    "args",
    [
        ["--exec=rm -rf /"],
        ["--EXEC", "id"],
        ["--exec-b", "id"],  # abbreviation of --exec-before-download
        ["--batch-fil", "urls.txt"],
        ["--print-to", "x", "f"],
        ["--outp", "x"],
        ["-ixo", "/tmp/out"],  # cluster: -i -x -o /tmp/out
        ["-o/tmp/x"],
        ["-Pdir"],
        ["-a-"],
        ["-f", "best", "--exec", "id"],
        ["-U"],
    ],
)
def test_ytdlp_tricky_forms(args: list[str]) -> None:
    with pytest.raises(UnsafeArgument):
        check_ytdlp_args(args)


@pytest.mark.parametrize(
    "args",
    [
        ["--exec-a", "id"],  # abbreviation of --exec-after
        ["--down", "a.db"],
        ["-qDfoo"],
        ["-o", "postprocessors=[]"],
        ["--filter", "__import__('os').system('id')"],
        ["--print", "\fE __import__('os')"],
        ["--Print", "x"],
        ["-Nx"],
        ["--config-open"],
        ["-X", "/tmp/extractors"],
    ],
)
def test_gallerydl_tricky_forms(args: list[str]) -> None:
    with pytest.raises(UnsafeArgument):
        check_gallerydl_args(args)


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["-f", "bv*[height<=720]+ba/b"],
        ["-fbest"],  # value of -f, not a cluster with -b -e -s -t
        ["-S", "res,codec"],
        ["--limit-rate", "1M", "--limit-rate=2M"],
        ["--print", "title"],
        ["--netrc"],
        ["--no-exec", "--no-config-locations", "--no-batch-file"],
        ["--output-na-placeholder", "none"],
        ["--sleep-interval", "5", "-i"],
        ["--", "https://example.org/x"],
        ["--match-filters", "!is_live"],
    ],
)
def test_ytdlp_allowed(args: list[str]) -> None:
    assert check_ytdlp_args(args) == args


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--range", "1-20"],
        ["--sleep", "2.0-3.5", "-q"],
        ["-u", "me", "-p", "secret"],
        ["--date-after", "2026-01-01"],
        ["--no-part", "--no-skip", "-v"],
        ["-a", "Mozilla/5.0"],  # -a is --user-agent in gallery-dl
        ["--config-ignore"],
    ],
)
def test_gallerydl_allowed(args: list[str]) -> None:
    assert check_gallerydl_args(args) == args


def test_non_strings_and_nul_are_refused() -> None:
    with pytest.raises(UnsafeArgument):
        check_ytdlp_args([1])  # type: ignore[list-item]
    with pytest.raises(UnsafeArgument):
        check_ytdlp_args(["-f\x00x"])


def test_check_urls() -> None:
    assert check_urls([" https://a ", "ytsearch5:cats"]) == ["https://a", "ytsearch5:cats"]
    for bad in ([], [""], ["--exec=id"], ["-o"]):
        with pytest.raises(ValueError):
            check_urls(bad)
