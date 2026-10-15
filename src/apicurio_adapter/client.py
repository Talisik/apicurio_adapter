"""Reading schemas from an Apicurio Registry (v3 API) and resolving their references."""

import copy
import os
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from typing import Any

import httpx

API = "/apis/registry/v3"
FETCH_TIMEOUT = 5
#: A connection or name-lookup failure is retried this many times; an HTTP error answer is not.
ATTEMPTS = 5


class RegistryError(RuntimeError):
    pass


_default_group: str | None = None
_load_env: Callable[[], None] | None = None


def configure(
    default_group: str | None = None,
    load_env: Callable[[], None] | None = None,
) -> None:
    """
    Settings an application makes once, before it imports its models.

    default_group  group of a class that does not pass `group=` (REGISTRY_GROUP overrides it)
    load_env       called before every read of the environment, for an application that loads
                   its .env lazily (models are fetched at import, which can be earlier)
    """
    global _default_group, _load_env

    if default_group is not None:
        _default_group = default_group

    if load_env is not None:
        _load_env = load_env


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


def _get(group: str, path: str, what: str) -> httpx.Response:
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
    """The content and the references of one registry version. A version never changes, so this is cached."""
    what = f"{group}/{artifact}@{version}"
    base = f"{artifact}/versions/{version}"

    with ThreadPoolExecutor(max_workers=2) as pool:
        content = pool.submit(_get, group, f"{base}/content", what)
        references = pool.submit(
            _get, group, f"{base}/references", f"references of {what}"
        )

        return content.result().json(), references.result().json()


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
