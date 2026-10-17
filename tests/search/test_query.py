"""The query syntax: parse() into a SearchFilter, format_query() back, errors and warnings."""

from datetime import date

import pytest

from app.db.models_files import MediaKind
from app.search.filters import SearchFilter
from app.search.query import SearchSyntaxError, format_query, parse, tokenize
from tests.library.helpers import fake_archived, make_folder, set_tag
from tests.review.helpers import inbox

pytestmark = pytest.mark.usefixtures("clean_ingest")


@pytest.fixture
def tagged(clean_ingest, db_conn) -> None:
    """Tags that exist (unknown tags are warnings)."""
    [file_id] = fake_archived(db_conn, inbox(db_conn), 1)
    for name in ("cat", "dog", "red", "blue", "고양이", "artist:bob", "type:image", "-odd"):
        set_tag(db_conn, file_id, name, "user")


async def p(sessionmaker, query: str):
    async with sessionmaker() as session:
        return await parse(session, query)


async def f(sessionmaker, query: str) -> SearchFilter:
    return (await p(sessionmaker, query)).filter


async def test_tokens_respect_quotes() -> None:
    tokens = tokenize('a  "b c" folder:"x y"z -"d"')
    assert [t.text() for t in tokens] == ["a", "b c", "folder:x yz", "-d"]
    assert [t.start for t in tokens] == [0, 3, 9, 23]
    assert [t.whole_quoted for t in tokens] == [False, True, False, False]
    assert [t.text() for t in tokenize(r'"say \"hi\" \\ now"')] == ['say "hi" \\ now']


@pytest.mark.usefixtures("tagged")
async def test_every_token_type(sessionmaker, db_conn) -> None:
    animals = make_folder(db_conn, "Animals")
    cats = make_folder(db_conn, "Big Cats", animals)
    flt = await f(
        sessionmaker,
        'Cat -dog ~red ~Blue folder:"animals/big cats" collected:2026-01-01..2026-03-31'
        " archived:..2026-05-01 type:image TYPE:video source:ytdlp source:upload status:pending"
        ' status:discarded "quoted words"',
    )
    assert flt.tags_all == ["cat"]
    assert flt.tags_none == ["dog"]
    assert flt.tags_any == ["red", "blue"]
    assert (flt.folder_id, flt.folder_path, flt.include_subfolders) == (
        cats,
        "Animals/Big Cats",
        True,
    )
    assert (flt.collected_from, flt.collected_to) == (date(2026, 1, 1), date(2026, 3, 31))
    assert (flt.archived_from, flt.archived_to) == (None, date(2026, 5, 1))
    assert flt.media_kinds == [MediaKind.image, MediaKind.video]
    assert flt.source_keys == ["ytdlp", "upload"]
    assert flt.statuses == ["pending_review", "discarded"]
    assert flt.text == ["quoted words"]


async def test_defaults_and_exact_folder(sessionmaker, db_conn) -> None:
    empty = await f(sessionmaker, "   ")
    assert empty == SearchFilter()
    assert empty.statuses == ["archived"]
    flt = await f(sessionmaker, "folder=:Inbox")
    assert (flt.folder_id, flt.include_subfolders) == (inbox(db_conn), False)
    open_start = await f(sessionmaker, "collected:2026-02-01..")
    assert (open_start.collected_from, open_start.collected_to) == (date(2026, 2, 1), None)
    one_day = await f(sessionmaker, "archived:2026-02-03")
    assert (one_day.archived_from, one_day.archived_to) == (date(2026, 2, 3), date(2026, 2, 3))


@pytest.mark.usefixtures("tagged")
async def test_korean_and_namespaced_tags(sessionmaker) -> None:
    parsed = await p(sessionmaker, "고양이 Artist:Bob -작가:홍길동 +type:image ~-odd")
    assert parsed.filter.tags_all == ["고양이", "artist:bob", "type:image"]
    assert parsed.filter.tags_none == ["작가:홍길동"]
    assert parsed.filter.tags_any == ["-odd"]
    # Unknown tags are warnings with the position of the tag.
    assert [(w.message, w.position) for w in parsed.warnings] == [
        ('no file has the tag "작가:홍길동"', 16)
    ]


async def test_unknown_tags_are_warnings(sessionmaker) -> None:
    parsed = await p(sessionmaker, "ghost ~spirit -phantom")
    assert parsed.filter.tags_all == ["ghost"]
    assert [w.position for w in parsed.warnings] == [0, 7, 15]


@pytest.mark.parametrize(
    ("query", "position", "message"),
    [
        ('cat "open', 4, "unterminated quote"),
        ("folder:Nowhere", 7, 'unknown folder "Nowhere"'),
        ('folder:"Inbox" folder=:Inbox', 15, "folder can be given only once"),
        ("type:picture", 5, 'unknown type "picture"'),
        ("status:gone", 7, 'unknown status "gone"'),
        ("collected:2026-13-01", 10, "not a valid date"),
        ("collected:2026-01-01..soon", 22, '"soon" is not a date'),
        ("archived:..", 9, "needs a start or an end"),
        ("archived:2026-02-01..2026-01-01", 9, "ends before it starts"),
        ("archived:2026-01-01 archived:2026-02-01", 20, "archived can be given only once"),
        ("cat -", 4, 'a tag must follow "-"'),
        ("~___", 1, "is not a valid tag"),
        ("x source:", 9, "source: needs a value"),
    ],
)
async def test_errors_carry_positions(
    sessionmaker, query: str, position: int, message: str
) -> None:
    with pytest.raises(SearchSyntaxError) as info:
        await p(sessionmaker, query)
    assert info.value.position == position
    assert message in info.value.message


@pytest.mark.usefixtures("tagged")
@pytest.mark.parametrize(
    "query",
    [
        'cat -dog folder:"Animals" collected:2026-01-01..2026-12-31 type:image',
        '~red ~blue folder=:"Animals/Big Cats" archived:..2026-05-01 status:discarded',
        'collected:2026-01-01.. "a \\"quoted\\" phrase" "second" source:"my source" source:upload',
        "고양이 artist:bob +type:image -작가:홍길동 ~-odd status:pending status:archived",
        "archived:2026-03-03 type:audio type:other",
        "",
    ],
)
async def test_round_trip(sessionmaker, db_conn, query: str) -> None:
    animals = make_folder(db_conn, "Animals")
    make_folder(db_conn, "Big Cats", animals)
    first = await f(sessionmaker, query)
    text = format_query(first)
    again = await f(sessionmaker, text)
    assert again.conditions() == first.conditions()
    assert format_query(again) == text  # format is canonical


async def test_format_canonical_form(sessionmaker, db_conn) -> None:
    make_folder(db_conn, "Animals")
    flt = await f(sessionmaker, 'TYPE:Image "x"  folder:animals status:archived Cat')
    assert format_query(flt) == 'cat folder:"Animals" type:image "x"'
