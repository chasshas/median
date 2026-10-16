import httpx

from app.db.engine import create_engine, database_ok


async def test_healthz_reports_database_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


async def test_database_ok_false_when_unreachable() -> None:
    engine = create_engine("postgresql://nobody:nothing@127.0.0.1:1/none")
    try:
        assert await database_ok(engine) is False
    finally:
        await engine.dispose()
