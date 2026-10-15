"""
Pydantic models and enums whose fields come from an Apicurio Registry.

    class Engagements(RegistryModel, artifact="Engagements", version=2):
        def interactions(self) -> int: ...

The fields are built from the registry schema of that artifact version; the
class body only adds behaviour (methods, computed fields). The group is the
configured default unless the class passes `group=`. The version is required:
a registry version never changes, so a pinned version is the same schema every
time, and an unreviewed edit in the registry cannot change a running service.

Computed fields are not built from the schema (the export marks them
`readOnly`): the class body defines them. Fields that are never exported (local
state such as an `exclude=True` field) also stay in the class body.

A model that nests other registry models or enums passes them in `refs`, so a
nested `#/definitions/PGFactPost` is the project's own `PGFactPost` class:

    class PGExecutions(RegistryModel, artifact="PGExecutions", version=1, refs={"PGFactPost": PGFactPost}):
        ...

An enum is built the same way. The schema's `enum` holds the values; the
member names come from `x-enum-names` (value -> name) and, for a value that is
not listed there, from the value itself (`"No publish date"` -> `NO_PUBLISH_DATE`):

    class ArticlePlatformKind(str, RegistryEnum, artifact="ArticlePlatformKind", version=1):
        @classmethod
        def from_url(cls, url: str): ...

The schema is fetched when the class is defined, that is, at import. If it
cannot be fetched (registry unreachable, wrong credentials, version missing),
the import fails with a RegistryError and the application does not start. There
is no fallback copy.
"""

import enum
import re
import types
import typing
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from json_schema_to_pydantic import create_model
from pydantic import BaseModel
from pydantic._internal._model_construction import ModelMetaclass

from .client import RegistryError, default_group, fetch_schema


@dataclass(frozen=True)
class LoadedModel:
    model: str  # the class name
    artifact: str
    group: str
    version: int
    fields: int


#: Every RegistryModel / RegistryEnum built so far in this process, in the order built.
LOADED: list[LoadedModel] = []


def without_read_only(node: Any) -> Any:
    """The schema minus computed fields (`readOnly` properties), at every depth."""
    if isinstance(node, dict):
        out = {k: without_read_only(v) for k, v in node.items()}
        properties = out.get("properties")

        if isinstance(properties, dict):
            dropped = {
                k
                for k, v in properties.items()
                if isinstance(v, dict) and v.get("readOnly")
            }
            out["properties"] = {
                k: v for k, v in properties.items() if k not in dropped
            }

            if "required" in out:
                out["required"] = [k for k in out["required"] if k not in dropped]

        return out

    if isinstance(node, list):
        return [without_read_only(v) for v in node]

    return node


def _load(artifact: str, version: int | None, group: str | None, who: str) -> tuple:
    if version is None:
        raise RegistryError(
            f'{who} needs a version: class {who}(..., artifact="{artifact}", version=<n>)'
        )

    group = group or default_group()

    return fetch_schema(artifact, version, group), group, version


def _use_given(tp: Any, given: dict[str, Any]) -> Any:
    """`tp` with every library-built class named in `given` replaced by the given class.

    json-schema-to-pydantic honours the classes passed in `refs` for a plain
    `$ref` but builds its own copy for one inside an optional (`anyOf` with null).
    """
    if (
        isinstance(tp, type)
        and tp.__module__.startswith("json_schema_to_pydantic")
        and tp.__name__ in given
    ):
        return given[tp.__name__]

    args = typing.get_args(tp)

    if not args:
        return tp

    new = tuple(_use_given(a, given) for a in args)

    if new == args:
        return tp

    origin = typing.get_origin(tp)

    if origin is typing.Union or origin is types.UnionType:
        return typing.Union[new]  # noqa: UP007  (built at runtime from a tuple)

    return tp.copy_with(new) if hasattr(tp, "copy_with") else origin[new]


def _is_model(value: Any) -> bool:
    return isinstance(value, type) and issubclass(value, BaseModel)


class _RegistryMeta(ModelMetaclass):
    def __new__(
        mcls,
        name: str,
        bases: tuple,
        namespace: dict[str, Any],
        artifact: str | None = None,
        version: int | None = None,
        group: str | None = None,
        refs: dict[str, Any] | None = None,
        **kwargs: Any,
    ):
        if artifact is not None:
            schema, group, version = _load(artifact, version, group, name)
            predefined = {
                f"#/definitions/{key}": value for key, value in (refs or {}).items()
            }
            built = create_model(
                without_read_only(schema),
                predefined_models={k: v for k, v in predefined.items() if _is_model(v)}
                or None,
                predefined_refs={
                    k: v for k, v in predefined.items() if not _is_model(v)
                }
                or None,
                allow_undefined_type=True,
                allow_undefined_array_items=True,
            )
            given = dict(refs or {})

            if given:
                for field in built.model_fields.values():
                    field.annotation = _use_given(field.annotation, given)

                built.model_rebuild(force=True)

            bases = tuple(built if base is RegistryModel else base for base in bases)

        model = super().__new__(mcls, name, bases, namespace, **kwargs)

        if artifact is not None:
            LOADED.append(
                LoadedModel(name, artifact, group, version, len(model.model_fields))
            )

        return model


class RegistryModel(BaseModel, metaclass=_RegistryMeta):
    """Base class for models whose fields live in the schema registry."""

    if TYPE_CHECKING:
        # Fields are built from the registry at import, which editors cannot see;
        # this stops them from reporting every field access as an error.
        def __getattr__(self, name: str) -> Any: ...


def enum_members(schema: dict[str, Any]) -> list[tuple]:
    """(member name, value) pairs of an enum schema, in the schema's order."""
    names = schema.get("x-enum-names") or {}

    return [
        (names.get(value) or re.sub(r"\W+", "_", str(value)).strip("_").upper(), value)
        for value in schema.get("enum", [])
    ]


class _RegistryEnumMeta(enum.EnumMeta):
    if TYPE_CHECKING:
        # Members are created from the registry at import, which editors cannot
        # see; this stops them from reporting every member access as an error.
        def __getattr__(cls, name: str) -> Any: ...

    def __new__(
        mcls,
        name,
        bases,
        classdict,
        artifact: str | None = None,
        version: int | None = None,
        group: str | None = None,
        **kwargs: Any,
    ):
        if artifact is not None:
            schema, group, version = _load(artifact, version, group, name)

            for member, value in enum_members(schema):
                classdict[member] = value

        cls = super().__new__(mcls, name, bases, classdict, **kwargs)

        if artifact is not None:
            LOADED.append(LoadedModel(name, artifact, group, version, len(cls)))

        return cls


class RegistryEnum(enum.Enum, metaclass=_RegistryEnumMeta):
    """Base class for enums whose members live in the schema registry."""
