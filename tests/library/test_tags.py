# ruff: noqa: RUF001 - full-width and ligature characters are the point of these tests
import pytest

from app.library.tags import MAX_TAG_LENGTH, normalize_tag, normalize_tags, split_tag_input


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Landscape", "landscape"),
        ("  blue   sky  ", "blue_sky"),
        ("blue\t\nsky", "blue_sky"),
        ("_under_score_", "under_score"),
        ("  _ spaced _ ", "spaced"),
        ("ＡＢＣ", "abc"),  # full-width letters, NFKC
        ("ｶﾀｶﾅ", "カタカナ"),  # half-width katakana, NFKC
        ("ﬁle", "file"),  # ligature, NFKC
        ("Ⅻ", "xii"),  # roman numeral, NFKC + lowercase
        ("Artist:Some One", "artist:some_one"),
        ("artist : some one", "artist:some_one"),
        ("ns_1:Value", "ns_1:value"),
        ("풍경 사진", "풍경_사진"),
        ("작가:홍길동", "작가:홍길동"),  # namespace must be [a-z0-9_]+, so kept as plain text
        ("a:b:c", "a:b:c"),
        ("http://x", "http://x"),
    ],
)
def test_normalize_tag(raw: str, expected: str) -> None:
    assert normalize_tag(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "___", " _ _ ", "artist:", "artist:  _ "])
def test_empty_results_are_dropped(raw: str) -> None:
    assert normalize_tag(raw) is None


def test_length_limit() -> None:
    tag = normalize_tag("x" * 150)
    assert tag == "x" * MAX_TAG_LENGTH


def test_length_limit_trims_trailing_underscore() -> None:
    tag = normalize_tag("a" * 99 + " b")
    assert tag == "a" * 99


def test_normalize_tags_dedupes_in_order() -> None:
    assert normalize_tags(["B", "a", " b ", "", "A"]) == ["b", "a"]


def test_split_tag_input() -> None:
    assert split_tag_input("a, b ,, c") == ["a", " b ", " c"]
    assert split_tag_input(None) == []
