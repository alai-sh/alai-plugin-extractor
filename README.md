# alai-plugin-extractor

> [!WARNING]
> This is an internal component of alai, not a public library. It's
> available to use, but its interface and behaviour can change in any
> release without warning or deprecation. Pin an exact version if you depend
> on it.

Static extraction of plugin metadata from Ansible collections: the modules,
plugins, filters, tests, roles and playbooks a collection provides, their
`short_description` and, optionally, their full docs, plus the plugin
routing from `meta/runtime.yml`.

Collections are treated as untrusted input. Nothing is imported or executed:
names come from file paths and, for filters and tests, the Python AST; docs
come from string constants (`DOCUMENTATION`, `EXAMPLES`, `RETURN`) or a YAML
sidecar, parsed with a safe loader.

Used by `galaxy-sync` to build the alai plugin index, and intended for the
alai client to read the same metadata from collections that aren't in an
index. Storing results, and deciding when to extract again, is up to the
caller. Requires Python 3.12 or later.

```python
import alai_plugin_extractor as ape

collection = ape.extract("ansible_collections/ns/coll", docs=True)
print(collection.namespace, collection.name, collection.version)
for plugin in collection.plugins:
    print(plugin.type, plugin.name, plugin.short_description)

# Apply doc fragments, from this collection and any others available.
builtin = ape.extract("~/dev/ansible", type="ansible", docs=True)
lookup = ape.fragment_lookup([collection, builtin])
plugin = collection.plugins[0]
docs = plugin.docs.apply_fragments(lookup, collection="ns.coll", module=plugin.type == "module")
docs.fragments  # any names lookup couldn't supply
```

## Public API

Everything is imported from `alai_plugin_extractor`. Results are frozen
dataclasses; the YAML read from a collection inside them stays plain dicts
and lists.

### Functions

```python
def extract(
    path: os.PathLike[str] | str,
    *,
    type: SourceType = "collection",
    docs: bool = False,
) -> Collection
```

Extract from a directory or a tarball file, detected automatically. A
tarball's members are read as needed; nothing is written to disk. `type` is
what `path` holds:

- `"collection"`: a collection directory or tarball.
- `"ansible"`: an ansible-core source checkout (a directory), extracted as
  `ansible.builtin` at the version in `lib/ansible/release.py`.

With `docs`, each plugin's raw docs and the collection's doc fragments are
read too. Raises `OSError` if `path` can't be read, `tarfile.TarError` if
it's a file that isn't a tarball, and `ValueError` for an unknown `type` or
one that needs a directory.

```python
def extract_bytes(
    data: bytes | bytearray | memoryview,
    *,
    type: SourceType = "collection",
    docs: bool = False,
) -> Collection
```

Extract from a tarball in memory. Only `"collection"` can be read from a
tarball for now. Raises `tarfile.TarError` if `data` isn't a tarball, and
`ValueError` for any other `type`.

`SourceType` is `Literal["collection", "ansible"]`, a plain string so it's
easy to pass from configuration or a command line. More types may be added
(legacy roles, for instance); an unknown one raises `ValueError` rather than
being read as a collection.

```python
def fragment_lookup(
    collections: Iterable[Collection],
) -> FragmentLookup
```

A lookup over the doc fragments of `collections` (extracted with
`docs=True`) for `PluginDocs.apply_fragments()`. It follows their
`doc_fragments` routing as ansible-core does: a redirect is followed into
any of `collections`, and a tombstone means the fragment was removed.
`ansible.builtin` redirects most short names from Ansible 2.9 (`azure` ->
`azure.azcollection.azure`), so include it. Collections whose name is
unknown are skipped.

### `Collection`

| Field | Type | |
|---|---|---|
| `namespace`, `name`, `version` | `str \| None` | From `MANIFEST.json`, or `galaxy.yml` in a source checkout. `None` if missing or malformed |
| `dependencies` | `Mapping[str, str]` | Collection name to version range |
| `requires_ansible` | `str \| None` | From `meta/runtime.yml` |
| `plugins` | `tuple[Plugin, ...]` | Sorted by `(type, name)` |
| `routing` | `tuple[Routing, ...]` | `plugin_routing` entries for plugin types, from `meta/runtime.yml`, and `extensions/eda/eda_runtime.yml` for event sources and filters. Sorted by `(type, name)` |
| `fragments` | `Mapping[str, DocFragment]` | Doc fragments the collection defines, by name. `docs=True` only |
| `fragment_routing` | `Mapping[str, Mapping[str, object]]` | `plugin_routing.doc_fragments` entries, by name. `docs=True` only |
| `issues` | `tuple[Issue, ...]` | What couldn't be read fully |

### `Plugin`

| Field | Type | |
|---|---|---|
| `type` | `str` | The `ansible-doc -t` name (`module`, `lookup`, `filter`, ...), `role`, `playbook`, or an Event-Driven Ansible type: `event_source`, `event_filter`, `rulebook` |
| `name` | `str` | Short name. A subdirectory adds a prefix: `plugins/modules/net/ping.py` is `net.ping` |
| `short_description` | `str \| None` | Whitespace collapsed to single spaces |
| `docs` | `PluginDocs \| None` | With `docs=True`, if the plugin has any |

Modules include action plugins without a module of the same name. Filters
and tests are the names their file defines, read from the AST. Event sources,
event filters and rulebooks are read from every directory ansible-rulebook
searches (`extensions/eda/...` and the older `plugins/event_source` and
`rulebooks`); when a name is in more than one, the one ansible-rulebook loads
wins.

### `PluginDocs`

| Field | Type | |
|---|---|---|
| `documentation` | `Mapping[str, object] \| None` | `DOCUMENTATION`, without `extends_documentation_fragment` |
| `examples` | `str \| None` | `EXAMPLES`, the source text |
| `returns` | `Mapping[str, object] \| None` | `RETURN`, without `extends_documentation_fragment` |
| `entry_points` | `Mapping[str, object] \| None` | A role's `meta/argument_specs.yml`, by entry point: `main` is `tasks/main.yml`, any other a task file run with `tasks_from` |
| `fragments` | `tuple[str, ...]` | `DOCUMENTATION`'s `extends_documentation_fragment`, still to apply |
| `return_fragments` | `tuple[str, ...]` | `RETURN`'s `extends_documentation_fragment`, still to apply |

```python
def apply_fragments(
    self,
    lookup: FragmentLookup,
    *,
    collection: str | None = None,
    module: bool = False,
) -> PluginDocs
```

These docs with every fragment `lookup` can supply applied, merged as
ansible-core does: the plugin's own values win, options and attributes
merge per entry, `notes` and `seealso` are extended, other lists are
combined. Where ansible-core would fail, the plugin's value is kept. Names
`lookup` can't supply stay in `fragments` / `return_fragments`, so the
result can be applied again when more collections are available.

With `collection` (the plugin's, `"ns.coll"`), each `version_added`, removal
version and removal date is labelled with the collection it belongs to, as
`ansible-doc` does (`version_added_collection`, `removed_from_collection`,
`collection_name`): the plugin's own from `collection`, a fragment's from
the fragment's collection. Pass `module=True` for modules, whose
deprecations are shaped differently. Without `collection`, nothing is
labelled.

### `DocFragment`

| Field | Type | |
|---|---|---|
| `collection` | `str \| None` | The defining collection, `"ns.coll"`. `None` if its name is unknown |
| `name` | `str` | Within the collection: `auth`, or `sub.auth` in a subdirectory |
| `attributes` | `Mapping[str, Mapping[str, object]]` | `ModuleDocFragment`'s parsed attributes: `DOCUMENTATION`, `RETURN` and any other uppercase name, which `ns.coll.auth.other` refers to |

`FragmentLookup` is `Callable[[str], DocFragment | None]`. It's called with
a fragment's full name (`ns.coll.name`; short names are `ansible.builtin`'s)
and returns the fragment, or `None` if it isn't available. Write your own to
look fragments up some other way, such as lazily from an index.

### `Routing`

| Field | Type | |
|---|---|---|
| `type`, `name` | `str` | The plugin it's for, which the collection may no longer ship |
| `entry` | `Mapping[str, object]` | Verbatim: `redirect`, `deprecation`, `tombstone`, ... |

### `Issue`

| Field | Type | |
|---|---|---|
| `kind` | `IssueKind` | See below |
| `path` | `str` | The file within the collection |
| `detail` | `str` | e.g. which value was too large |

`IssueKind` is one of `"python_unparseable"` (often Python 2),
`"filter_test_unresolved"` (filter or test names that can't be found
statically), `"fragment_unresolved"` (a doc fragment built at runtime), or
`"value_too_large"` (a YAML value past the limits below, dropped).

## YAML

Every value read from YAML can be written as standard JSON, for example with
`json.dumps(dataclasses.asdict(collection))`:

- Dates and `!!binary` stay strings, as ansible-core treats them.
- `.nan` and `.inf` become strings.
- A tag the loader doesn't know (`!unsafe`, `!vault`, a collection's own,
  `!!python/...`) is ignored and the value kept as written; so is a standard
  tag whose value doesn't fit it (`!!int abc`).
- A value past 100,000 nodes or 64 levels deep (an alias bomb) is dropped,
  with a `value_too_large` issue.

New keys in docs, specs and routing pass through unchanged.

YAML parsing uses libyaml when PyYAML was built with it, and falls back to
the pure Python loader otherwise.

## Command line

```bash
alai-plugin-extractor ansible_collections/ns/coll         # JSON
alai-plugin-extractor ns-coll-1.0.0.tar.gz --format text  # one line per plugin
alai-plugin-extractor ns-coll-1.0.0.tar.gz --docs         # JSON with docs and fragments
alai-plugin-extractor ~/dev/ansible --type ansible        # ansible.builtin from a checkout
```

Issues are printed to stderr as warnings.
