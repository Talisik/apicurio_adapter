"""Reading schemas from an Apicurio Registry (v3 API) and resolving their references."""

import copy
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("apicurio_adapter")

API = "/apis/registry/v3"
FETCH_TIMEOUT = 5
#: A connection or name-lookup failure is retried this many times; an HTTP error answer is not.
ATTEMPTS = 5


class RegistryError(RuntimeError):
    pass


_default_group: str | None = None
_load_env: Callable[[], None] | None = None
_snapshot_dir: Path | None = None

#: Where each registry version was read from in this process: "registry" or "snapshot".
SOURCES: dict[tuple[str, str, int], str] = {}
_references: dict[tuple[str, str, int], list] = {}
_warned = False  # the first fallback is logged as a warning, the rest quietly

#: Set when the registry stayed unreachable through every retry: the rest of this process goes
#: straight to the snapshots instead of waiting out the retries again for every version.
_registry_down = False


def configure(
    default_group: str | None = None,
    load_env: Callable[[], None] | None = None,
    snapshot_dir: str | Path | None = None,
) -> None:
    """
    Settings an application makes once, before it imports its models.

    default_group  group of a class that does not pass `group=` (REGISTRY_GROUP overrides it)
    load_env       called before every read of the environment, for an application that loads
                   its .env lazily (models are fetched at import, which can be earlier)
    snapshot_dir   folder in the application's repo holding a copy of every schema version it
                   loads (REGISTRY_SNAPSHOT_DIR overrides it). The registry is always tried
                   first; a version it cannot supply is read from here instead. Every version
                   fetched from the registry is saved here when its file is missing.
    """
    global _default_group, _load_env, _snapshot_dir

    if default_group is not None:
        _default_group = default_group

    if load_env is not None:
        _load_env = load_env

    if snapshot_dir is not None:
        _snapshot_dir = Path(snapshot_dir)


def _env(name: str) -> str | None:
    if _load_env is not None:
        _load_env()

    return os.environ.get(name)


def registry_url() -> str:
    return (_env("REGISTRY_URL") or "http://localhost:8080").rstrip("/")


def default_group() -> str:
    group = _env("REGISTRY_GROUP") or _default_group

    if not group:
        raise RegistryError(
            "No registry group: set REGISTRY_GROUP, call configure(default_group=...), "
            "or pass group= to the class"
        )

    return group


def snapshot_dir() -> Path | None:
    configured = _env("REGISTRY_SNAPSHOT_DIR")

    return Path(configured) if configured else _snapshot_dir


def snapshot_path(group: str, artifact: str, version: int) -> Path | None:
    directory = snapshot_dir()

    return directory / group / f"{artifact}@{version}.json" if directory else None


def _read_snapshot(group: str, artifact: str, version: int) -> tuple | None:
    path = snapshot_path(group, artifact, version)

    if path is None or not path.is_file():
        return None

    try:
        saved = json.loads(path.read_text(encoding="utf-8"))

        return saved["content"], saved["references"]
    except (OSError, ValueError, KeyError) as e:
        log.warning("Ignoring the unreadable snapshot %s: %s", path, e)

        return None


def _write_snapshot(
    group: str, artifact: str, version: int, content: Any, references: Any
) -> None:
    """Save a version the registry supplied. A version never changes, so an existing file is kept."""
    path = snapshot_path(group, artifact, version)

    if path is None or path.exists():
        return

    try:
        path.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.NamedTemporaryFile(
            "w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
        ) as tmp:
            json.dump(
                {
                    "group": group,
                    "artifact": artifact,
                    "version": version,
                    "content": content,
                    "references": references,
                },
                tmp,
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
            tmp.write("\n")

        os.replace(tmp.name, path)
    except (
        OSError
    ) as e:  # a read-only image, for one: the registry copy is still in use
        log.debug("Could not save the snapshot %s: %s", path, e)


def _get(group: str, path: str, what: str) -> httpx.Response:
    global _registry_down

    if _registry_down:
        raise RegistryError(f"The schema registry was unreachable earlier ({what})")

    client_id = _env("REGISTRY_CLIENT_ID")
    client_secret = _env("REGISTRY_CLIENT_SECRET")
    url = f"{registry_url()}{API}/groups/{group}/artifacts/{path}"

    for attempt in range(1, ATTEMPTS + 1):
        try:
            res = httpx.get(
                url,
                auth=(client_id, client_secret)
                if client_id and client_secret
                else None,
                timeout=FETCH_TIMEOUT,
            )
            break
        except httpx.TransportError as e:
            if attempt == ATTEMPTS:
                _registry_down = True

                raise RegistryError(
                    f"Cannot reach the schema registry for {what}: {e}"
                ) from e

            # a brief DNS or network hiccup should not stop a service starting
            time.sleep(attempt)

    if res.status_code != 200:
        raise RegistryError(
            f"Registry returned {res.status_code} for {what}: {res.text[:200]}"
        )

    return res


def _json(res: httpx.Response, what: str) -> Any:
    try:
        return res.json()
    except ValueError as e:  # a proxy's error page with status 200, for one
        raise RegistryError(f"Registry sent invalid JSON for {what}: {e}") from e


def _rewrite_refs(node: Any, targets: dict[str, str]) -> Any:
    """`node` with every `$ref` named in `targets` replaced by its target."""
    if isinstance(node, dict):
        return {
            k: targets.get(v, v)
            if k == "$ref" and isinstance(v, str)
            else _rewrite_refs(v, targets)
            for k, v in node.items()
        }

    if isinstance(node, list):
        return [_rewrite_refs(v, targets) for v in node]

    return node


@cache
def _version(group: str, artifact: str, version: int) -> tuple:
    """
    The content and the references of one version. A version never changes, so this is cached.

    The registry is asked first. When it cannot answer (unreachable, timed out, refusing, or
    sending nonsense) the snapshot in the application's repo is used, if there is one.
    """
    what = f"{group}/{artifact}@{version}"
    base = f"{artifact}/versions/{version}"
    key = (group, artifact, int(version))

    global _warned

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            content = pool.submit(_get, group, f"{base}/content", what)
            references = pool.submit(
                _get, group, f"{base}/references", f"references of {what}"
            )

            fetched = (
                _json(content.result(), what),
                _json(references.result(), f"references of {what}"),
            )
    except RegistryError as e:
        snapshot = _read_snapshot(*key)

        if snapshot is None:
            path = snapshot_path(*key)

            raise RegistryError(
                f"{e}; no snapshot of {what}"
                + (f" at {path}" if path else " (no snapshot folder is configured)")
            ) from e

        # one warning for the process; the rest of the versions are logged quietly
        log.log(
            logging.DEBUG if _warned else logging.WARNING,
            "Schema registry unavailable (%s); using the snapshot of %s",
            e,
            what,
        )
        _warned = True
        SOURCES[key] = "snapshot"
        _references[key] = snapshot[1]

        return snapshot

    SOURCES[key] = "registry"
    _references[key] = fetched[1]
    _write_snapshot(*key, *fetched)

    return fetched


def source_of(group: str, artifact: str, version: int) -> str:
    """ "snapshot" if this version, or any version it references, was read from a snapshot.

    Only versions already loaded count; this never asks the registry.
    """
    key = (group, artifact, int(version))
    sources = {SOURCES.get(key, "registry")} | {
        source_of(r["groupId"], r["artifactId"], int(r["version"]))
        for r in _references.get(key, [])
    }

    return "snapshot" if "snapshot" in sources else "registry"


def _resolved(
    group: str, artifact: str, version: int, stack: tuple = ()
) -> dict[str, Any]:
    """
    The schema of one registry version, with the versions it references folded in.

    The registry keeps a reference as `{"$ref": "<group>.<Artifact>.json"}` plus a
    list naming the artifact version behind it. Each referenced schema is placed
    under `definitions/<Artifact>` and the `$ref` points there, so the result is
    one self-contained schema however deep the references go.
    """
    key = (group, artifact, int(version))

    if key in stack:
        raise RegistryError(
            f"Circular reference: {' -> '.join(f'{a}@{v}' for _, a, v in (*stack, key))}"
        )

    schema, references = _version(*key)

    if not references:
        return schema

    definitions: dict[str, Any] = dict(schema.get("definitions") or {})
    targets: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=len(references)) as pool:
        children = list(
            pool.map(
                lambda reference: _resolved(
                    reference["groupId"],
                    reference["artifactId"],
                    int(reference["version"]),
                    (*stack, key),
                ),
                references,
            )
        )

    for reference, resolved in zip(references, children):
        name = reference["artifactId"]
        child = copy.deepcopy(resolved)
        child.pop("$schema", None)

        for def_name, definition in {
            **(child.pop("definitions", None) or {}),
            name: child,
        }.items():
            if definitions.setdefault(def_name, definition) != definition:
                raise RegistryError(
                    f"{group}/{artifact}@{version} needs two different versions of {def_name}"
                )

        targets[reference["name"]] = f"#/definitions/{name}"

    return _rewrite_refs({**schema, "definitions": definitions}, targets)


def fetch_schema(
    artifact: str, version: int, group: str | None = None
) -> dict[str, Any]:
    """The schema of one registry version. Raises RegistryError when unavailable."""
    return copy.deepcopy(_resolved(group or default_group(), artifact, int(version)))


def latest_version(artifact: str, group: str | None = None) -> int:
    group = group or default_group()

    return int(
        _get(group, f"{artifact}/versions/branch=latest", f"{group}/{artifact}").json()[
            "version"
        ]
    )
