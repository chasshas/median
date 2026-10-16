"""The Procrastinate app. Only app/jobs/ imports Procrastinate directly."""

import procrastinate

from app.config import get_settings, libpq_url


def make_connector(database_url: str) -> procrastinate.PsycopgConnector:
    return procrastinate.PsycopgConnector(conninfo=libpq_url(database_url))


job_app = procrastinate.App(
    connector=make_connector(get_settings().database_url),
    import_paths=["app.jobs.tasks"],
)
