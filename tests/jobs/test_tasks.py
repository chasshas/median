from app.jobs.tasks import ping


async def test_ping_task_replies_pong() -> None:
    assert await ping("hello") == "pong"
