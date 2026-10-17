from collections.abc import Iterator

import pytest

from app.crawl import registry as registry_module
from app.crawl.registry import Registry
from tests.crawl.addons import make_registry


@pytest.fixture
def registry(clean_ingest) -> Iterator[Registry]:
    """The test add-ons as the process-wide registry (used by the run_crawl task)."""
    reg = make_registry()
    registry_module.set_registry(reg)
    yield reg
    registry_module.set_registry(None)


@pytest.fixture
async def crawl_app(app, registry):
    app.state.registry = registry
    return app
