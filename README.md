# apicurio-adapter

Pydantic models and enums built from the schemas in an [Apicurio Registry](https://www.apicur.io/registry/)
(v3 API). The registry is the source of truth; the class in your code only adds behaviour.

```python
from apicurio_adapter import RegistryEnum, RegistryModel


class Engagements(RegistryModel, artifact="Engagements", version=2):
    def interactions(self) -> int:  # methods and computed fields stay in code
        return self.likes_count + self.shares_count


class Platform(str, RegistryEnum, artifact="Platform", version=1):
    @classmethod
    def from_url(cls, url: str): ...
```

## How it works

- The schema of `artifact` at `version` is fetched **when the class is defined**, so when it is
  imported. If the registry cannot supply it (unreachable, wrong credentials, missing version) the
  import raises `RegistryError` and the application does not start. There is no fallback copy.
- The version is required. A registry version never changes, so a pinned class always gets the same
  schema, and an edit in the registry changes nothing until someone bumps the number in code.
- Properties marked `readOnly` are computed fields: they are left to the class body.
- A model that nests other registry models passes them in `refs`, so a nested `#/definitions/Author`
  is your own `Author` class, not a copy:

  ```python
  class Article(
      RegistryModel, artifact="Article", version=8, refs={"Author": Author}
  ): ...
  ```

- Enums take their values from the schema's `enum`, and member names from `x-enum-names`
  (value → name) or from the value itself (`"No publish date"` → `NO_PUBLISH_DATE`).
- Registry **references** (`{"$ref": "<group>.<Artifact>.json"}` plus a pinned child version) are
  followed however deep they go and folded into one schema. Circular references, and a chain that
  needs two different versions of one artifact, are errors.
- A connection or name-lookup failure is retried 5 times; an HTTP error answer is not. Fetched
  versions are cached for the life of the process.
- **Snapshots.** Point the adapter at a folder in your repo (`configure(snapshot_dir=...)` or
  `REGISTRY_SNAPSHOT_DIR`). The registry is always asked first, and every version it supplies is
  saved there as `<group>/<Artifact>@<version>.json` when the file is missing (a version never
  changes, so a saved file is never rewritten). Commit the folder. When the registry cannot answer
  (unreachable, timed out, an error status or unreadable reply), the saved copy is used instead and a
  warning is logged. Once the registry has stayed unreachable through the retries, the rest of the
  process skips straight to the snapshots. With no snapshot for a version the import fails, as before.
  `LOADED[i].source` says `"registry"` or `"snapshot"` for each class.

## Configuration

| Environment variable     | Meaning                                                  |
| ------------------------ | -------------------------------------------------------- |
| `REGISTRY_URL`           | default `http://localhost:8080`                          |
| `REGISTRY_GROUP`         | group of a class that does not pass `group=`             |
| `REGISTRY_CLIENT_ID`     | HTTP Basic user, if the registry needs one               |
| `REGISTRY_CLIENT_SECRET` | HTTP Basic password                                      |
| `REGISTRY_SNAPSHOT_DIR`  | snapshot folder; overrides `configure(snapshot_dir=...)` |

Call `configure` once, before the models are imported, for what the environment should not decide:

```python
import apicurio_adapter

apicurio_adapter.configure(
    default_group="my.group",  # used when REGISTRY_GROUP is not set
    load_env=load_my_dotenv,  # runs before every read of the environment
)
```

`load_env` is for applications that read their `.env` lazily: models are fetched at import, which
can happen before that.

`apicurio_adapter.LOADED` lists every model built so far (class, artifact, group, version, field
count), for a startup log.

## Development

```
uv sync
uv run python -m unittest discover -s tests
```
