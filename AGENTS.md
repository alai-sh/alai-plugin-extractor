# alai-plugin-extractor

Static extraction of plugin metadata from Ansible collections, shared by
`../galaxy-sync` (building the plugin index) and the future alai client. See
[README.md](README.md) for the API. Supports Python 3.12+ (the client runs on
Ansible control nodes, and ansible-core 2.20 needs 3.12); development uses
3.15 (standard GIL build, pinned in `.python-version`). The only runtime
dependency is `pyyaml`. Not published yet: galaxy-sync depends on it through
a uv path source.

It's internal to alai, not a public library (the README says so). "Public
API" below means what galaxy-sync and the client use: it can change in any
release without a deprecation period, but every change must update
galaxy-sync and the README in the same piece of work.

## Commands

```bash
uv run pytest                     # tests
uv run ruff check .               # lint
uv run ruff format . --check      # formatting
uv run basedpyright --warnings    # type check (warnings are failures)
uv run tox run -e sanity          # the three checks above
uv run tox                        # sanity plus tests on 3.12-3.15; fails below 100% coverage
uv run alai-plugin-extractor PATH # extract a local collection dir or tarball (--format text)
```

All checks must pass before work is done, on every supported Python:
basedpyright checks against 3.12, so don't use newer stdlib APIs or syntax
(e.g. 3.14's unparenthesized `except A, B:`). Tooling config follows
`~/dev/msauthlib`. CI (`.github/workflows/ci.yml`) runs sanity, then the
tests against the built wheel on Linux and macOS for each version. Actions
are pinned to commit SHAs with the tag in a comment; Dependabot updates
them. After changing this package, also run galaxy-sync's checks,
since it consumes the API and the stored output.

## Style

- Standard library imports are `import x`, not `from x import y`; the
  exceptions are `import typing as _t` and `import collections.abc as _c`.
  Imports within the package use `from ._x import y`.
- Public functions taking a path accept `os.PathLike[str] | str`.
- Public functions and methods put each parameter on its own line, with the
  return type on the closing `) -> T:` line; the trailing comma keeps ruff
  from joining them. The README's "Public API" signatures match.
- Choices callers pass are `typing.Literal` aliases (`SourceType`,
  `IssueKind`), not enums, and unknown values are rejected at runtime.

## Layout

- `src/alai_plugin_extractor/__init__.py` - the public API: `extract()`,
  `extract_bytes()` (both take `type`, a `SourceType` literal: add new
  values there and reject unknown ones), `fragment_lookup()` and the result
  types, documented in
  the README's "Public API" section (keep it in step). Everything else is
  private, including the sources.
- `src/alai_plugin_extractor/_extract.py` - extraction, the YAML loader and
  JSON-safety.
- `src/alai_plugin_extractor/_sources.py` - `DirSource`, `TarSource` and
  `AnsibleCoreSource`, the read-only views of a collection.
- `src/alai_plugin_extractor/_fragments.py` - `DocFragment` and the merge
  behind `PluginDocs.apply_fragments()`, a port of ansible-core's
  `add_fragments`, `merge_fragment` and `add_collection_to_versions_and_dates`.
  It imports nothing from `_types`, which imports it.
- `src/alai_plugin_extractor/_types.py` - the frozen result dataclasses and
  `fragment_lookup()`.
- `src/alai_plugin_extractor/_cli.py` - the `alai-plugin-extractor` command
  (also `python -m alai_plugin_extractor`). The only module allowed to `print`.
- `tests/test_extract.py` - every edge case, built from in-memory fixtures
  through the private `_extract._extract()`.
- `tests/test_fragments.py` - fragment merging.
- `tests/test_cli.py` - the command line.

## Rules

- **Never import or execute collection code.** Names come from paths and the
  Python AST, YAML only goes through the safe `_Loader`, and `DirSource` /
  `TarSource` never read outside the collection.
- **Extraction only.** Storage, versioning stored results and deciding when
  to extract again belong to the caller. Note any change that alters output
  for the same input (a fixed edge case, different descriptions) in the
  release notes: galaxy-sync bumps its `EXTRACTION_FORMAT` on those.
- Every YAML value in a result must serialise as standard JSON. `_Loader` keeps
  dates (`removal_date: 2024-01-01`, implicit or `!!timestamp`) and
  `!!binary` as strings, and `_json_value` converts the rest and bounds the
  size, since a few hundred bytes of aliases expand to millions of nodes.
- An odd YAML tag must never lose the document. `_Loader` reads a tag it
  doesn't know (`!unsafe`, `!vault`, `!!python/...`) as if untagged, and a
  standard tag that fails to construct (`!!int abc`) likewise, which used to
  crash extraction. Unknown keys always pass through.
- Docs stay raw: extraction never applies `extends_documentation_fragment`.
  Fragments often live in other collections, so callers merge them with
  `apply_fragments()` and whatever collections they have. Its merge rules
  follow ansible-core's `lib/ansible/utils/plugin_docs.py`; where ansible-core
  raises, the plugin's own value is kept. After changing the merge or the
  labelling, compare with ansible-core over the corpus: every plugin's docs,
  through ansible-core's `add_collection_to_versions_and_dates` and
  `add_fragments` with a loader serving the same fragments.
- Event-Driven Ansible content follows what ansible-rulebook loads
  (`ansible_rulebook/collection.py`): every directory it searches, in its
  order, first one wins; only files directly inside (FQCN can't name a
  subdirectory or a dotted name); rulebooks only as `.yml`; routing only
  from `extensions/eda/eda_runtime.yml`. ansible-rulebook's own
  `eda.builtin` plugins live in ansible-rulebook, not a collection.
- A name the extractor can't see must make a filter/test file *unresolved*,
  never be silently dropped from an otherwise resolved list. The same goes
  for doc fragments: one whose attributes are built at runtime is an issue.

## Testing

The package must stay at **100% line and branch coverage**; tox fails below
it (`fail_under = 100`). It runs over thousands of arbitrary, untrusted
collections, and the odd ones (legacy encodings, Python 2, broken symlinks,
doc stubs, dynamic filter maps) are exactly where it goes wrong quietly: a
dropped plugin or a missing description, not a crash. Every branch exists
for a real case, so every branch needs a test that shows it.

- Any change comes with tests for each new branch. When a case is found on
  Galaxy, add a test that reproduces it and name the collection in a comment.
- Build fixtures in memory (`MemSource`, `_tarball`) or under `tmp_path`.
  Don't copy real collections into the repo.
- Don't use `# pragma: no cover`. If a branch can't be reached, remove it.
- Confirm a new test fails without the change it covers.
- For a change that could shift results broadly, also run galaxy-sync's
  `extract` command over `~/dev/galaxy-collections/latest/*` and compare the
  summary with the findings below.

## Findings from real collections

Over the latest version of all 4,576 Galaxy collections (2026-10), extraction
takes about 2 minutes on one core with libyaml.

- Filter/test names resolve statically in 909 of 915 files (99.3%),
  including a class-level `filter_map` returned conditionally.
- Descriptions are missing for 12% of modules, 78% of filters (few have doc
  sidecars), 88% of roles (few have `argument_specs`) and all playbooks
  (playbooks have no doc format).
- 13 Python files don't parse (mostly Python 2); their plugins are listed
  without descriptions. (Earlier runs reported 17 because a broken `.py` doc
  stub beside a `.ps1` module was counted twice.)
- Python 3.15 rejects invalid UTF-8 anywhere in a file, including comments
  (e.g. a GBK comment in `h3c_bmc.hdm`'s `hdm_redfish_command.py`); older
  versions only reject it in code and strings. `_parse_python` retries with
  the bad bytes replaced.
- Results can differ by interpreter: a plugin using syntax newer than the
  running Python doesn't parse and loses its description. galaxy-sync runs
  the newest version, so the index is the best case; a 3.12 client may see
  less for such plugins.
- Windows and action-backed modules (`ansible.windows`, `microsoft.ad`) ship a
  `.py` doc stub or `.ps1` beside a `.yml` sidecar. Some action-only modules
  have only the `plugins/modules/<name>.yml` sidecar (`capable.core212`).
- With `docs=True`, 60,418 of 83,463 entries have docs (45,631 with
  EXAMPLES, 44,681 with RETURN, 2,295 roles with `argument_specs`). The
  largest value is 15,100 nodes (`brightcomputing.bcm92`'s `cloud_node`) and
  the deepest 24 levels, well inside the limits. Extraction takes about 1.6x
  as long, and the docs JSON is 266 MB in all (largest collection 11.8 MB).
- 1,064 doc fragments; 9 files are unresolved: `dalibo.pglift` and
  `infisical.vault` build them with calls, `amazon.aws`'s `boto3` and `tags`
  set some or all in `__init__`, and `mglantz.demo_collection`'s doesn't
  parse. One
  (`jomrr.ca`) uses an annotated `DOCUMENTATION: str =`.
- Merging and labelling match ansible-core for all 102,795 comparable
  DOCUMENTATION and RETURN sections; ansible-core rejects 6 more outright.
  (The one difference found was a fragment's empty `notes:`, which
  ansible-core drops.)
- Applying fragments from every latest collection plus `ansible.builtin`
  with `fragment_lookup()` resolves 94.3% of 46,265 references in about 2
  seconds (92.2% before `doc_fragments` routing was followed: ansible-core
  redirects 112 short names such as `azure`). The rest are real gaps: forks
  bundling old copies of modules whose fragments were since renamed or
  removed (`anophelesgreyhoe.general` for `community.general`, which renamed
  them `_attributes` and so on in 13; `remyj38.vmware` and others for
  `community.vmware`, which has none in 7.0), short names that were never
  redirected (`management_center`), and `amazon.aws.boto3`.
- Event-Driven Ansible: 74 collections keep event sources in
  `extensions/eda/plugins/event_source` (165 files) and 20 in the older
  `plugins/event_source`; 4 ship both (`chris93111.nats_eda`). Rulebooks
  are in `extensions/eda/rulebooks` (80 collections) or a top-level
  `rulebooks` (8); 3 are `.yaml` and not loadable. Only `ansible.eda` has an
  `eda_runtime.yml`. Reading them all added 275 index lines (137 rulebooks,
  123 event sources, 15 event filters).
- Two documents were lost to YAML tags before unknown tags were read as
  plain values: `prometheus.prometheus`'s `process_exporter` role has a
  `!unsafe` default in its `argument_specs`, and `famedly.matrix`'s
  `matrix_room` has a Matrix room ID, `sample: !asdf...:server.tld`, which
  YAML parses as a tag.
- `ansible.builtin`: 261 entries plus 3,978 routing-only lines from
  `ansible_builtin_runtime.yml`.
