"""
Run with: uv run python -m unittest discover -s tests

Every test fakes the registry; none needs a network.
"""

import os
import unittest
from unittest import mock

import httpx
from pydantic import ValidationError

from apicurio_adapter import RegistryEnum, RegistryError, RegistryModel, client, model

client.configure(default_group="test.group")


PERSON = {
    "type": "object",
    "title": "Person",
    "properties": {
        "name": {"type": "string"},
        "age": {"type": "integer", "minimum": 0},
        "nick": {"type": "string", "default": "none"},
    },
    "required": ["name", "age"],
    "additionalProperties": False,
}


def fake_registry(versions):
    """A stand-in for registry_model._get: versions = {(artifact, version): (content, references)}."""

    def get(group, path, what):
        artifact, _, version, kind = path.split("/")
        content, references = versions[(artifact, int(version))]
        return httpx.Response(200, json=content if kind == "content" else references)

    return get


def reference(artifact, version):
    return {
        "groupId": "g",
        "artifactId": artifact,
        "version": str(version),
        "name": f"g.{artifact}.json",
    }


class ReferenceTest(unittest.TestCase):
    def setUp(self):
        client._version.cache_clear()

    def fetch(self, versions, artifact="Parent", version=1):
        with mock.patch.object(client, "_get", fake_registry(versions)):
            return client.fetch_schema(artifact, version, "g")

    def test_a_reference_becomes_a_definition(self):
        schema = self.fetch(
            {
                ("Parent", 1): (
                    {"title": "Parent", "properties": {"c": {"$ref": "g.Child.json"}}},
                    [reference("Child", 1)],
                ),
                ("Child", 1): ({"title": "Child", "type": "object"}, []),
            }
        )

        self.assertEqual(schema["properties"]["c"], {"$ref": "#/definitions/Child"})
        self.assertEqual(schema["definitions"]["Child"]["title"], "Child")

    def test_references_are_followed_as_deep_as_they_go(self):
        schema = self.fetch(
            {
                ("Parent", 1): (
                    {"properties": {"c": {"$ref": "g.Child.json"}}},
                    [reference("Child", 1)],
                ),
                ("Child", 1): (
                    {"properties": {"g": {"$ref": "g.Grandchild.json"}}},
                    [reference("Grandchild", 1)],
                ),
                ("Grandchild", 1): ({"title": "Grandchild"}, []),
            }
        )

        self.assertEqual(sorted(schema["definitions"]), ["Child", "Grandchild"])
        self.assertEqual(
            schema["definitions"]["Child"]["properties"]["g"],
            {"$ref": "#/definitions/Grandchild"},
        )

    def test_two_versions_of_one_artifact_are_refused(self):
        with self.assertRaisesRegex(RegistryError, "two different versions of Leaf"):
            self.fetch(
                {
                    ("Parent", 1): (
                        {},
                        [reference("A", 1), reference("B", 1)],
                    ),
                    ("A", 1): ({}, [reference("Leaf", 1)]),
                    ("B", 1): ({}, [reference("Leaf", 2)]),
                    ("Leaf", 1): ({"title": "one"}, []),
                    ("Leaf", 2): ({"title": "two"}, []),
                }
            )

    def test_a_circular_reference_is_refused(self):
        with self.assertRaisesRegex(RegistryError, "Circular"):
            self.fetch(
                {
                    ("Parent", 1): ({}, [reference("Child", 1)]),
                    ("Child", 1): ({}, [reference("Parent", 1)]),
                }
            )


class RegistryModelTest(unittest.TestCase):
    def setUp(self):
        client._version.cache_clear()  # versions are cached; each test fakes its own registry

    def test_builds_the_model_from_the_registry_and_keeps_behaviour(self):
        with mock.patch.object(model, "fetch_schema", return_value=PERSON) as fetch:

            class Person(RegistryModel, artifact="Person", version=2):
                def shout(self) -> str:
                    return self.name.upper()

        fetch.assert_called_once_with("Person", 2, client.default_group())
        self.assertEqual(Person(name="ada", age=36).shout(), "ADA")
        self.assertEqual(Person(name="ada", age=1).nick, "none")

    def test_a_class_can_name_its_own_group(self):
        with mock.patch.object(model, "fetch_schema", return_value=PERSON) as fetch:

            class Person(RegistryModel, artifact="Person", version=1, group="other"):
                pass

        fetch.assert_called_once_with("Person", 1, "other")
        self.assertEqual(model.LOADED[-1].group, "other")

    def test_the_default_group_comes_from_the_environment(self):
        with mock.patch.dict(os.environ, {"REGISTRY_GROUP": "team.a"}):
            self.assertEqual(client.default_group(), "team.a")

    def test_records_what_was_loaded(self):
        with mock.patch.object(model, "fetch_schema", return_value=PERSON):

            class Person(RegistryModel, artifact="Person", version=2):
                pass

        self.assertEqual(
            model.LOADED[-1],
            model.LoadedModel("Person", "Person", client.default_group(), 2, 3),
        )

    def test_a_missing_version_is_an_error(self):
        with self.assertRaisesRegex(RegistryError, "needs a version"):

            class Person(RegistryModel, artifact="Person"):
                pass

        with self.assertRaisesRegex(RegistryError, "needs a version"):

            class Kind(RegistryEnum, artifact="Kind"):
                pass

    def test_the_import_fails_when_the_registry_is_unreachable(self):
        with (
            mock.patch.object(client.time, "sleep"),
            mock.patch.object(
                httpx, "get", side_effect=httpx.ConnectError("refused")
            ) as get,
            self.assertRaisesRegex(RegistryError, "Cannot reach"),
        ):

            class Person(RegistryModel, artifact="Person", version=2):
                pass

        self.assertEqual(get.call_count, 2 * client.ATTEMPTS)  # content and references

    def test_a_brief_network_failure_is_retried(self):
        failed = set()

        def get(url, **kwargs):
            if url not in failed:  # each request fails once, then succeeds
                failed.add(url)
                raise httpx.ConnectError("name lookup failed")
            return httpx.Response(
                200, json=[] if url.endswith("/references") else PERSON
            )

        with (
            mock.patch.object(client.time, "sleep"),
            mock.patch.object(httpx, "get", side_effect=get) as calls,
        ):

            class Person(RegistryModel, artifact="Person", version=2):
                pass

        self.assertEqual(calls.call_count, 4)  # content and references, twice each
        self.assertIn("age", Person.model_fields)

    def test_an_error_answer_is_not_retried(self):
        with (
            mock.patch.object(
                httpx, "get", return_value=httpx.Response(401, text="no")
            ) as get,
            self.assertRaisesRegex(RegistryError, "401"),
        ):

            class Person(RegistryModel, artifact="Person", version=2):
                pass

        self.assertEqual(get.call_count, 2)  # content and references, each asked once

    def test_the_import_fails_when_the_registry_refuses(self):
        with (
            mock.patch.object(
                httpx, "get", return_value=httpx.Response(401, text="unauthorized")
            ),
            self.assertRaisesRegex(RegistryError, "401"),
        ):

            class Person(RegistryModel, artifact="Person", version=2):
                pass

    def test_the_import_fails_when_the_version_does_not_exist(self):
        with (
            mock.patch.object(
                httpx, "get", return_value=httpx.Response(404, text="not found")
            ),
            self.assertRaisesRegex(RegistryError, "404"),
        ):

            class Person(RegistryModel, artifact="Person", version=99):
                pass

    def test_the_schema_is_enforced_and_extra_input_ignored(self):
        with mock.patch.object(model, "fetch_schema", return_value=PERSON):

            class Person(RegistryModel, artifact="Person", version=2):
                pass

        with self.assertRaises(ValidationError):
            Person(name="ada", age=-1)
        with self.assertRaises(ValidationError):
            Person(name="ada")
        self.assertNotIn("extra", Person(name="ada", age=1, extra=1).model_dump())

    def test_computed_fields_are_left_to_the_class(self):
        schema = {
            **PERSON,
            "properties": {
                **PERSON["properties"],
                "shout": {"type": "string", "readOnly": True},
            },
        }
        with mock.patch.object(model, "fetch_schema", return_value=schema):

            class Person(RegistryModel, artifact="Person", version=2):
                @property
                def shout(self) -> str:
                    return self.name.upper()

        self.assertNotIn("shout", Person.model_fields)
        self.assertEqual(Person(name="ada", age=1).shout, "ADA")

    def test_nested_models_use_the_given_classes_even_inside_an_optional(self):
        team = {
            "type": "object",
            "title": "Team",
            "properties": {
                "members": {"type": "array", "items": {"$ref": "#/definitions/Person"}},
                "lead": {
                    "anyOf": [{"$ref": "#/definitions/Person"}, {"type": "null"}],
                    "default": None,
                },
            },
            "required": ["members"],
            "definitions": {"Person": PERSON},
        }
        with mock.patch.object(model, "fetch_schema", side_effect=[PERSON, team]):

            class Person(RegistryModel, artifact="Person", version=2):
                pass

            class Team(
                RegistryModel, artifact="Team", version=1, refs={"Person": Person}
            ):
                pass

        ada = Person(name="ada", age=36)
        self.assertIsInstance(Team(members=[ada], lead=ada).lead, Person)
        self.assertIsInstance(Team(members=[ada]).members[0], Person)

    def test_an_enum_is_built_from_values_and_names(self):
        schema = {
            "type": "string",
            "enum": [
                "No publish date",
                "Visibility is not public/null",
                "Missing field",
            ],
            "x-enum-names": {"Visibility is not public/null": "NOT_PUBLIC"},
        }
        with mock.patch.object(model, "fetch_schema", return_value=schema):

            class Kind(str, RegistryEnum, artifact="Kind", version=1):
                @property
                def shout(self) -> str:
                    return self.value.upper()

        self.assertEqual(
            [(m.name, m.value) for m in Kind],
            [
                ("NO_PUBLISH_DATE", "No publish date"),
                ("NOT_PUBLIC", "Visibility is not public/null"),
                ("MISSING_FIELD", "Missing field"),
            ],
        )
        self.assertIs(Kind("Missing field"), Kind.MISSING_FIELD)
        self.assertEqual(Kind.NOT_PUBLIC, "Visibility is not public/null")
        self.assertEqual(Kind.MISSING_FIELD.shout, "MISSING FIELD")


class ConfigurationTest(unittest.TestCase):
    def setUp(self):
        self.saved = (client._default_group, client._load_env)

    def tearDown(self):
        client._default_group, client._load_env = self.saved

    def test_without_any_group_the_class_cannot_be_built(self):
        client._default_group = None

        with (
            mock.patch.dict(os.environ, {}, clear=True),
            self.assertRaisesRegex(RegistryError, "No registry group"),
        ):
            client.default_group()

    def test_the_environment_wins_over_the_configured_group(self):
        client.configure(default_group="configured")

        with mock.patch.dict(os.environ, {"REGISTRY_GROUP": "from.env"}):
            self.assertEqual(client.default_group(), "from.env")

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(client.default_group(), "configured")

    def test_the_url_defaults_to_a_local_registry_and_loses_a_trailing_slash(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(client.registry_url(), "http://localhost:8080")

        with mock.patch.dict(os.environ, {"REGISTRY_URL": "https://r.example/"}):
            self.assertEqual(client.registry_url(), "https://r.example")

    def test_the_env_loader_runs_before_the_settings_are_read(self):
        calls = []
        client.configure(load_env=lambda: calls.append("loaded"))

        client.registry_url()

        self.assertEqual(calls, ["loaded"])

    def test_credentials_are_sent_as_basic_auth_only_when_both_are_set(self):
        answer = httpx.Response(200, json={})

        with (
            mock.patch.dict(
                os.environ, {"REGISTRY_CLIENT_ID": "u", "REGISTRY_CLIENT_SECRET": "p"}
            ),
            mock.patch.object(httpx, "get", return_value=answer) as get,
        ):
            client._get("g", "A/versions/1/content", "A@1")

        self.assertEqual(get.call_args.kwargs["auth"], ("u", "p"))

        with (
            mock.patch.dict(os.environ, {"REGISTRY_CLIENT_ID": "u"}, clear=True),
            mock.patch.object(httpx, "get", return_value=answer) as get,
        ):
            client._get("g", "A/versions/1/content", "A@1")

        self.assertIsNone(get.call_args.kwargs["auth"])


if __name__ == "__main__":
    unittest.main()
