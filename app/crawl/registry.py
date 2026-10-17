"""Add-on discovery: the media_archive.addons entry points and DATA_DIR/addons/*.py.

Discovery runs once per process: at web startup (app lifespan) and at worker startup (when the
worker imports the task modules). New or changed add-ons need a restart of web and worker.
A broken add-on or a duplicate key is recorded as an error and shown on /sources; it never
stops the app.
"""

import importlib.metadata
import importlib.util
import inspect
import logging
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from app.crawl.base import KEY_PATTERN, RESERVED_KEYS, Addon

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "media_archive.addons"
LOCAL_MODULE_PREFIX = "median_local_addons"


@dataclass(frozen=True)
class AddonInfo:
    addon: Addon
    origin: str  # "entry point <name> = <value>" or the file path

    @property
    def key(self) -> str:
        return self.addon.key

    @property
    def name(self) -> str:
        return self.addon.name or self.addon.key

    @property
    def version(self) -> str:
        return self.addon.version

    @property
    def description(self) -> str:
        return self.addon.description

    @property
    def config_model(self) -> type[BaseModel]:
        return self.addon.ConfigModel


@dataclass(frozen=True)
class AddonError:
    origin: str
    error: str
    key: str | None = None


@dataclass
class Registry:
    addons: dict[str, AddonInfo] = field(default_factory=dict)
    errors: list[AddonError] = field(default_factory=list)

    def get(self, key: str) -> AddonInfo | None:
        return self.addons.get(key)

    def add(self, candidate: object, origin: str) -> None:
        try:
            addon = _as_addon(candidate)
        except Exception as exc:
            self.errors.append(AddonError(origin, f"{type(exc).__name__}: {exc}"))
            return
        existing = self.addons.get(addon.key)
        if existing is not None:
            self.errors.append(
                AddonError(
                    origin,
                    f"duplicate add-on key {addon.key!r} (already provided by"
                    f" {existing.origin}); this one is ignored",
                    addon.key,
                )
            )
            return
        self.addons[addon.key] = AddonInfo(addon, origin)

    def sorted(self) -> list[AddonInfo]:
        return sorted(self.addons.values(), key=lambda a: a.key)


def _as_addon(candidate: object) -> Addon:
    """Accept an Addon subclass (instantiated without arguments) or an instance."""
    if inspect.isclass(candidate):
        if not issubclass(candidate, Addon):
            raise TypeError(f"{candidate.__qualname__} is not a subclass of Addon")
        addon = candidate()
    elif isinstance(candidate, Addon):
        addon = candidate
    else:
        raise TypeError(f"expected an Addon subclass or instance, got {type(candidate).__name__}")
    if not isinstance(addon.key, str) or not KEY_PATTERN.match(addon.key):
        raise ValueError(f"invalid add-on key {addon.key!r} (use 1-50 of a-z, 0-9 and _)")
    if addon.key in RESERVED_KEYS:
        raise ValueError(f"add-on key {addon.key!r} is reserved")
    if not (inspect.isclass(addon.ConfigModel) and issubclass(addon.ConfigModel, BaseModel)):
        raise TypeError("ConfigModel must be a Pydantic model class")
    if type(addon).run is Addon.run or not inspect.iscoroutinefunction(addon.run):
        raise TypeError("run(ctx, config) must be implemented as an async method")
    addon.ConfigModel.model_json_schema()  # fails early on a config the forms cannot describe
    return addon


def _short_traceback(exc: BaseException) -> str:
    lines = traceback.format_exception_only(type(exc), exc)
    return "".join(lines).strip()


def _load_entry_points(registry: Registry) -> None:
    for ep in sorted(
        importlib.metadata.entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name
    ):
        origin = f"entry point {ep.name} = {ep.value}"
        try:
            candidate = ep.load()
        except Exception as exc:
            logger.exception("loading add-on %s failed", origin)
            registry.errors.append(AddonError(origin, _short_traceback(exc)))
            continue
        registry.add(candidate, origin)


def _load_file(registry: Registry, path: Path) -> None:
    origin = str(path)
    module_name = f"{LOCAL_MODULE_PREFIX}.{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        if not hasattr(module, "ADDON"):
            raise AttributeError("the file defines no module-level ADDON")
        candidate = module.ADDON
    except BaseException as exc:  # a broken file may even call sys.exit()
        if isinstance(exc, KeyboardInterrupt):
            raise
        sys.modules.pop(module_name, None)
        logger.exception("loading add-on file %s failed", path)
        registry.errors.append(AddonError(origin, _short_traceback(exc)))
        return
    registry.add(candidate, origin)


def discover(addons_dir: Path | None) -> Registry:
    """Entry points first (in name order), then addons_dir/*.py (in file name order).

    The first add-on with a key wins; later ones with the same key are errors.
    """
    registry = Registry()
    _load_entry_points(registry)
    if addons_dir is not None and addons_dir.is_dir():
        for path in sorted(addons_dir.glob("*.py")):
            if not path.name.startswith(("_", ".")):
                _load_file(registry, path)
    for err in registry.errors:
        logger.warning("add-on error (%s): %s", err.origin, err.error)
    logger.info("add-ons loaded: %s", ", ".join(registry.addons) or "none")
    return registry


_registry: Registry | None = None


def get_registry() -> Registry:
    """The process-wide registry, discovered on first use."""
    global _registry
    if _registry is None:
        from app.storage import get_paths

        _registry = discover(get_paths().addons)
    return _registry


def set_registry(registry: Registry | None) -> None:
    """Replace the process-wide registry (tests); None forces a new discovery."""
    global _registry
    _registry = registry
