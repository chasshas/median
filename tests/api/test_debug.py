import httpx
import psycopg


async def test_ping_defers_task(client: httpx.AsyncClient, db_conn: psycopg.Connection) -> None:
    response = await client.post("/api/debug/ping")

    assert response.status_code == 202
    body = response.json()
    assert body["task"] == "ping"
    row = db_conn.execute(
        "SELECT task_name, status::text, args FROM procrastinate_jobs WHERE id = %s",
        (body["job_id"],),
    ).fetchone()
    assert row is not None
    task_name, status, args = row
    assert task_name == "ping"
    assert status == "todo"
    assert args == {"message": "ping from /api/debug/ping"}
