"""Static plugin extraction from a collection.

This runs over arbitrary, untrusted collections, so it never imports or
executes collection code. Names come from file paths, except filters and
tests, whose names are read from the Python AST. Docs come from string
constants (DOCUMENTATION, EXAMPLES, RETURN) or a YAML sidecar, parsed with a
safe loader.
"""

import ast
import collections.abc as _c
import contextlib
import dataclasses
import io
import json
import math
import os
import pathlib
import tarfile
import typing as _t
import warnings

import yaml

from ._fragments import DocFragment
from ._sources import AnsibleCoreSource, DirSource, Source, TarSource
from ._types import Collection, Issue, IssueKind, Plugin, PluginDocs, Routing, SourceType

# libyaml is much faster, but PyYAML builds without it when it's missing and
# then doesn't define CSafeLoader. Both share the Resolver API _Loader uses.
_SafeLoader: type[yaml.SafeLoader] = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

# plugins/<dir> -> plugin type. Directories not listed aren't referenced by
# name in playbooks (module_utils, doc_fragments, plugin_utils, terminal, ...).
_PLUGIN_DIRS = {
    "become": "become",
    "cache": "cache",
    "callback": "callback",
    "cliconf": "cliconf",
    "connection": "connection",
    "httpapi": "httpapi",
    "inventory": "inventory",
    "lookup": "lookup",
    "netconf": "netconf",
    "shell": "shell",
    "strategy": "strategy",
    "vars": "vars",
}

# meta/runtime.yml plugin_routing keys -> plugin type.
_ROUTING_TYPES = {"modules": "module", "filter": "filter", "test": "test", **_PLUGIN_DIRS}

# Event-Driven Ansible content: (directory, type, extension), in the order
# ansible-rulebook searches (ansible_rulebook/collection.py). The first
# directory holding a name is the one it loads. Only files directly inside
# can be named by FQCN, and rulebooks must be .yml.
_EDA_DIRS = (
    ("extensions/eda/plugins/event_source/", "event_source", ".py"),
    ("extensions/eda/plugins/event_sources/", "event_source", ".py"),
    ("plugins/event_source/", "event_source", ".py"),
    ("extensions/eda/plugins/event_filter/", "event_filter", ".py"),
    ("extensions/eda/plugins/event_filters/", "event_filter", ".py"),
    ("plugins/event_filter/", "event_filter", ".py"),
    ("extensions/eda/rulebooks/", "rulebook", ".yml"),
    ("rulebooks/", "rulebook", ".yml"),
)

# extensions/eda/eda_runtime.yml plugin_routing keys -> plugin type. ansible-rulebook
# reads EDA routing only from there, never from meta/runtime.yml.
_EDA_ROUTING_TYPES = {"event_source": "event_source", "event_filter": "event_filter"}

# Bounds on a parsed YAML value that is kept. Aliases let a few hundred bytes
# of YAML expand to millions of nodes when written as JSON, so a value past
# either limit is dropped. Real docs stay far below them.
_MAX_NODES = 100_000
_MAX_DEPTH = 64


def extract(
    path: os.PathLike[str] | str,
    *,
    type: SourceType = "collection",  # noqa: A002 - the name callers expect, as Plugin.type
    docs: bool = False,
) -> Collection:
    """Extract a collection from a directory or a tarball on disk.

    `type` is what `path` holds: "collection", a collection directory or
    tarball; or "ansible", an ansible-core source checkout (a directory),
    extracted as ansible.builtin. With `docs`, each plugin's raw docs and the
    collection's doc fragments are read as well.

    Raises OSError if `path` can't be read, tarfile.TarError if it's a file
    that isn't a tarball, and ValueError for a `type` that isn't known or
    can't be read from a tarball.
    """
    _check_type(type)
    p = pathlib.Path(path)
    if p.is_dir():
        source = DirSource(p)
        if type == "ansible":
            return _extract(AnsibleCoreSource(source), docs=docs, metadata=_core_metadata(source))
        return _extract(source, docs=docs)
    if type != "collection":
        raise ValueError(f"type={type!r} needs a directory, not {p}")
    with tarfile.open(p) as tar:
        return _extract(TarSource(tar), docs=docs)


def extract_bytes(
    data: bytes | bytearray | memoryview,
    *,
    type: SourceType = "collection",  # noqa: A002 - as in extract()
    docs: bool = False,
) -> Collection:
    """Extract a collection from a tarball in memory. Nothing is written to disk.

    Only type="collection" can be read from a tarball for now. Raises
    tarfile.TarError if `data` isn't a tarball and ValueError for any other
    `type`.
    """
    _check_type(type)
    if type != "collection":
        raise ValueError(f"type={type!r} needs a directory; use extract() with its path")
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return _extract(TarSource(tar), docs=docs)


def _check_type(value: str) -> None:
    # A Literal isn't checked at runtime, and a type from a newer release
    # mustn't be read as a collection.
    if value not in (known := _t.get_args(SourceType)):
        raise ValueError(f"unknown type {value!r}, expected one of: {', '.join(known)}")


def _parse_python(data: bytes) -> ast.Module | None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            return ast.parse(data)
        except (SyntaxError, ValueError):
            pass
        # Python 3.15 rejects invalid UTF-8 anywhere in a file, including
        # comments that older versions ignored, and some collections have
        # legacy-encoded comments. Retry with the bad bytes replaced; a declared
        # encoding was already honoured by the first attempt.
        try:
            return ast.parse(data.decode("utf-8-sig", errors="replace"))
        except (SyntaxError, ValueError):
            return None


class _Loader(_SafeLoader):
    """Safe loader whose results can be stored as JSON.

    Dates stay strings, as ansible-core treats them (`removal_date:
    2024-01-01` in routing), and so does binary. That holds for the implicit
    forms and for explicit `!!timestamp` / `!!binary` tags.

    A tag it doesn't know (`!unsafe`, `!vault`, a collection's own, or
    `!!python/...`) is ignored and the value read as written, rather than
    failing the whole document. So is a standard tag whose value doesn't fit
    it (`!!int abc`). Nothing is ever constructed as a Python object.
    """


def _untagged(loader: yaml.SafeLoader, node: yaml.Node) -> object:
    """The value of `node` with its tag ignored: a string, list or dict."""
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    scalar = _t.cast(yaml.ScalarNode, node)
    if scalar.tag.startswith("!") and not scalar.style and not scalar.value:  # plain: None, or "" from libyaml
        # Nothing follows the "tag", so it was meant as text: famedly.matrix
        # has a Matrix room ID, `sample: !asdf...:server.tld`.
        return scalar.tag
    return loader.construct_scalar(scalar)


def _or_untagged(
    construct: _c.Callable[[yaml.SafeLoader, yaml.Node], object],
) -> _c.Callable[[yaml.SafeLoader, yaml.Node], object]:
    def wrapped(loader: yaml.SafeLoader, node: yaml.Node) -> object:
        try:
            return construct(loader, node)
        except (yaml.YAMLError, ValueError, KeyError):
            return _untagged(loader, node)  # e.g. `!!int abc`, `!!bool maybe`

    return wrapped


_Loader.yaml_implicit_resolvers = {
    key: [r for r in resolvers if r[0] != "tag:yaml.org,2002:timestamp"]
    for key, resolvers in _SafeLoader.yaml_implicit_resolvers.items()
}
for _tag in ("tag:yaml.org,2002:timestamp", "tag:yaml.org,2002:binary"):
    _Loader.add_constructor(_tag, _SafeLoader.construct_scalar)
for _tag in ("null", "bool", "int", "float"):
    _tag = f"tag:yaml.org,2002:{_tag}"
    _Loader.add_constructor(_tag, _or_untagged(_SafeLoader.yaml_constructors[_tag]))
# None is PyYAML's fallback for any other tag, which the stubs don't allow in add_constructor.
_Loader.yaml_constructors[None] = _untagged


def _mapping(value: object) -> dict[str, object] | None:
    """`value` as a dict if it is one. Parsed YAML and JSON are untyped."""
    return _t.cast(dict[str, object], value) if isinstance(value, dict) else None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _safe_yaml(text: str | bytes) -> object:
    try:
        return yaml.load(text, Loader=_Loader)  # noqa: S506 - a (C)SafeLoader subclass
    except (yaml.YAMLError, RecursionError):
        # The pure Python loader recurses once per nesting level.
        return None


class _TooLarge(Exception):
    pass


def _json_key(key: object) -> str:
    if isinstance(key, str):
        return key
    if isinstance(key, bool) or key is None:
        return json.dumps(key)  # as json.dumps writes them as keys: true, null
    return str(key)


def _json_value(value: object) -> object:
    """A copy of parsed YAML that json.dumps writes as standard JSON.

    Raises _TooLarge past _MAX_NODES or _MAX_DEPTH.
    """
    nodes = 0

    def copy(v: object, depth: int) -> object:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_NODES or depth > _MAX_DEPTH:
            raise _TooLarge
        if isinstance(v, dict):
            return {_json_key(k): copy(x, depth + 1) for k, x in _t.cast(dict[object, object], v).items()}
        if isinstance(v, (list, tuple)):  # !!omap and !!pairs give tuples
            return [copy(x, depth + 1) for x in _t.cast(_c.Sequence[object], v)]
        if isinstance(v, set):  # !!set, which YAML defines as a mapping to nulls
            return dict.fromkeys(sorted(_json_key(k) for k in _t.cast(set[object], v)))
        if isinstance(v, float) and not math.isfinite(v):
            return str(v)  # .nan and .inf have no JSON form
        return v

    return copy(value, 0)


class _Issues(list[Issue]):
    def add(self, kind: IssueKind, path: str, detail: str = "") -> None:
        self.append(Issue(kind=kind, path=path, detail=detail))


def _json_mapping(value: object, path: str, detail: str, issues: _Issues) -> dict[str, object] | None:
    """`value` as a JSON-safe dict, or None if it isn't a dict or is too large."""
    if (mapping := _mapping(value)) is None:
        return None
    try:
        return _t.cast(dict[str, object], _json_value(mapping))
    except _TooLarge:
        issues.add("value_too_large", path, detail)
        return None


@dataclasses.dataclass
class _Raw:
    """Docs as read from one file, before any checking."""

    path: str
    doc: object = None
    examples: object = None
    returns: object = None
    entry_points: object = None


def _short_description(raw: _Raw) -> str | None:
    """short_description of the docs, or of a role's main entry point."""
    entry_points = _mapping(raw.entry_points)
    doc = _mapping(entry_points.get("main") if entry_points else raw.doc)
    if doc is not None:
        desc = doc.get("short_description")
        if isinstance(desc, str) and desc.strip():
            return " ".join(desc.split())
    return None


def _string_assignments(body: list[ast.stmt]) -> tuple[dict[str, str], set[str]]:
    """Single-name assignments in `body`: string constants by name (the first
    one wins), and the names only ever assigned something else.

    Anything but a constant would need evaluating, which extraction never does.
    """
    constants: dict[str, str] = {}
    other: set[str] = set()
    for node in body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(target, ast.Name) or target.id in constants:
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            constants[target.id] = value.value
            other.discard(target.id)
        else:
            other.add(target.id)
    return constants, other


def _python_docs(path: str, tree: ast.Module, full: bool) -> _Raw:
    """Docs of a plugin's Python file: DOCUMENTATION and, if `full`, EXAMPLES and RETURN."""
    constants, _ = _string_assignments(tree.body)
    raw = _Raw(path)
    if "DOCUMENTATION" in constants:
        raw.doc = _safe_yaml(constants["DOCUMENTATION"])
    if full:
        raw.examples = constants.get("EXAMPLES")
        if "RETURN" in constants:
            raw.returns = _safe_yaml(constants["RETURN"])
    return raw


def _sidecar_docs(source: Source, path: str) -> _Raw:
    """Docs of a YAML sidecar (plugins/<type>/<name>.yml)."""
    data = source.read(path)
    loaded = (_mapping(_safe_yaml(data)) if data else None) or {}
    return _Raw(path, loaded.get("DOCUMENTATION"), loaded.get("EXAMPLES"), loaded.get("RETURN"))


def _fragment_names(doc: dict[str, object] | None) -> tuple[str, ...]:
    """Remove and return `extends_documentation_fragment`, a list or a comma-separated string."""
    value = doc.pop("extends_documentation_fragment", None) if doc else None
    names = (
        value.split(",") if isinstance(value, str) else _t.cast(list[object], value) if isinstance(value, list) else []
    )
    return tuple(stripped for name in names if isinstance(name, str) and (stripped := name.strip()))


def _plugin_docs(raw: _Raw, issues: _Issues) -> PluginDocs | None:
    doc = _json_mapping(raw.doc, raw.path, "DOCUMENTATION", issues)
    returns = _json_mapping(raw.returns, raw.path, "RETURN", issues)
    entry_points = _json_mapping(raw.entry_points, raw.path, "argument_specs", issues)
    examples = raw.examples if isinstance(raw.examples, str) and raw.examples.strip() else None
    if doc is None and returns is None and entry_points is None and examples is None:
        return None
    return PluginDocs(
        documentation=doc,
        examples=examples,
        returns=returns,
        entry_points=entry_points,
        fragments=_fragment_names(doc),
        return_fragments=_fragment_names(returns),
    )


def _first_docs(candidates: _c.Iterable[_Raw], full: bool, issues: _Issues) -> tuple[str | None, PluginDocs | None]:
    """The first short_description and, if `full`, the first non-empty docs
    among `candidates`, in priority order. Candidates are only read as far as
    needed, so a sidecar isn't opened when the Python file has everything.
    """
    desc: str | None = None
    docs: PluginDocs | None = None
    for raw in candidates:
        desc = desc or _short_description(raw)
        if full and docs is None:
            docs = _plugin_docs(raw, issues)
        if desc and (docs is not None or not full):
            break
    return desc, docs


def _module_candidates(source: Source, stem: str, py: _Raw | None) -> _c.Iterator[_Raw]:
    """A module's doc sources: its Python file (or action plugin), then a
    plugins/modules/<stem>.yml or .yaml sidecar."""
    if py is not None:
        yield py
    for side in ("yml", "yaml"):
        yield _sidecar_docs(source, f"plugins/modules/{stem}.{side}")


def _role_candidates(source: Source, role: str) -> _c.Iterator[_Raw]:
    for meta in ("meta/argument_specs.yml", "meta/argument_specs.yaml", "meta/main.yml", "meta/main.yaml"):
        path = f"roles/{role}/{meta}"
        spec = _mapping(_safe_yaml(source.read(path) or b""))
        if spec and (arg_specs := _mapping(spec.get("argument_specs"))) is not None:
            yield _Raw(path, entry_points=arg_specs)


def _fragment_attributes(tree: ast.Module) -> tuple[dict[str, str], bool]:
    """String attributes of a doc fragment's ModuleDocFragment class, and
    whether it may have any that couldn't be read.

    Only uppercase names count: ansible-core looks up `ns.coll.frag.var` as
    the attribute `VAR`, and plain `ns.coll.frag` as DOCUMENTATION. A method
    can set attributes at runtime (amazon.aws's boto3 builds them all in
    `__init__`), so a class with one, or with nothing readable, is unresolved.
    """
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ModuleDocFragment":
            constants, other = _string_assignments(node.body)
            attributes = {k: v for k, v in constants.items() if k.isupper()}
            has_method = any(isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) for item in node.body)
            return attributes, not attributes or has_method or any(k.isupper() for k in other)
    return {}, True


def _dict_keys(expr: ast.expr, known: dict[str, list[str] | None]) -> list[str] | None:
    """Statically resolve the string keys of a dict expression, or None."""
    if isinstance(expr, ast.Dict):
        keys: list[str] = []
        for k in expr.keys:
            if not (isinstance(k, ast.Constant) and isinstance(k.value, str)):
                return None  # ** spread or a computed key
            keys.append(k.value)
        return keys
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Name) and expr.func.id == "dict":
        if expr.args or any(kw.arg is None for kw in expr.keywords):
            return None
        return [kw.arg for kw in expr.keywords if kw.arg]
    if (name := _variable(expr)) is not None:
        return known.get(name)
    return None


def _variable(expr: ast.expr) -> str | None:
    """The `known` key for a local (`x`) or instance attribute (`self.x`)."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute) and isinstance(expr.value, ast.Name) and expr.value.id == "self":
        return f"self.{expr.attr}"
    return None


def _merge(known: dict[str, list[str] | None], name: str, extra: list[str] | None) -> None:
    current = known.get(name)
    known[name] = None if extra is None or current is None else current + extra


def _walk_body(body: list[ast.stmt], known: dict[str, list[str] | None], returns: list[list[str] | None]) -> None:
    for stmt in body:
        if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
            target = stmt.targets[0]
            if (name := _variable(target)) is not None:
                known[name] = _dict_keys(stmt.value, known)
            elif isinstance(target, ast.Subscript) and (name := _variable(target.value)) is not None:
                # x["literal"] = ... adds a known key. Any other key can't be
                # resolved, so the whole dict becomes unknown rather than
                # silently missing a name.
                key = target.slice
                literal = [key.value] if isinstance(key, ast.Constant) and isinstance(key.value, str) else None
                _merge(known, name, literal)
        elif isinstance(stmt, ast.AugAssign) and (name := _variable(stmt.target)) is not None:
            _merge(known, name, _dict_keys(stmt.value, known) if isinstance(stmt.op, ast.BitOr) else None)
        elif (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Call)
            and isinstance(stmt.value.func, ast.Attribute)
            and stmt.value.func.attr == "update"
            and (name := _variable(stmt.value.func.value)) is not None
        ):
            # x.update({...}), x.update(other) and x.update(a=..., b=...).
            call = stmt.value
            extra = _dict_keys(call.args[0], known) if len(call.args) == 1 else [] if not call.args else None
            if extra is not None and any(kw.arg is None for kw in call.keywords):
                extra = None  # x.update(**other)
            elif extra is not None:
                extra = extra + [kw.arg for kw in call.keywords if kw.arg]
            _merge(known, name, extra)
        elif isinstance(stmt, ast.Return) and stmt.value is not None:
            returns.append(_dict_keys(stmt.value, known))
        elif isinstance(stmt, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
            for block in ("body", "orelse", "finalbody"):
                _walk_body(getattr(stmt, block, []) or [], known, returns)
            for handler in getattr(stmt, "handlers", []):
                _walk_body(handler.body, known, returns)


def _returned_keys(func: ast.FunctionDef, class_attrs: dict[str, list[str] | None]) -> list[str] | None:
    """Keys of the dict returned by FilterModule.filters() / TestModule.tests().

    Handles `return {...}`, `return dict(...)`, class attributes returned as
    `self.x`, and locals built from those plus `x.update({...})` /
    `x["k"] = ...`. Returns inside if/try blocks are followed, and the union
    of every return that resolves is used (a fallback branch like
    `return {f: ... for f in self.filter_map}` doesn't add names). If no
    return resolves, the names are unresolved.
    """
    known = {f"self.{k}": v for k, v in class_attrs.items()}
    returns: list[list[str] | None] = []
    _walk_body(func.body, known, returns)
    resolved = [r for r in returns if r is not None]
    if not resolved:
        return None
    return sorted({k for r in resolved for k in r})


def _filter_test_names(tree: ast.Module, kind: str) -> list[str] | None:
    cls_name, method = ("FilterModule", "filters") if kind == "filter" else ("TestModule", "tests")
    module_level: dict[str, list[str] | None] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            module_level[node.targets[0].id] = _dict_keys(node.value, module_level)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == cls_name:
            class_attrs = {
                item.targets[0].id: _dict_keys(item.value, module_level)
                for item in node.body
                if isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name)
            }
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == method:
                    keys = _returned_keys(item, class_attrs)
                    if keys is None:
                        # A module-level dict returned by name, e.g. `return FILTERS`.
                        returns: list[list[str] | None] = []
                        _walk_body(item.body, dict(module_level), returns)
                        resolved = [r for r in returns if r is not None]
                        keys = sorted({k for r in resolved for k in r}) if resolved else None
                    return keys
    return None


def _plugin_name(rel: str) -> str:
    # plugins/modules/a/b.py -> "a.b" (collections allow subdirectories).
    return rel.rsplit(".", 1)[0].replace("/", ".")


def _runtime(source: Source, candidates: tuple[str, ...]) -> tuple[str, dict[str, object] | None]:
    """The first of `candidates` that exists, and its contents if they're a mapping."""
    for path in candidates:
        if (raw := source.read(path)) is not None:
            return path, _mapping(_safe_yaml(raw))
    return candidates[0], None


def _routing_entries(
    runtime: dict[str, object] | None, path: str, types: _c.Mapping[str, str], issues: _Issues
) -> _c.Iterator[tuple[str, str, dict[str, object]]]:
    """(type, name, entry) for each usable plugin_routing entry of the types in `types`."""
    plugin_routing = _mapping(runtime.get("plugin_routing")) if runtime else None
    for key, entries in (plugin_routing or {}).items():
        if (ptype := types.get(key)) and (entries_map := _mapping(entries)):
            for name, entry in entries_map.items():
                name = _json_key(name)  # YAML keys can be numbers
                if entry_map := _json_mapping(entry, path, f"{ptype}:{name}", issues):
                    yield ptype, name, entry_map


_Metadata = tuple[str | None, str | None, str | None, dict[str, str]]


def _metadata(source: Source) -> _Metadata:
    """Namespace, name, version and dependencies from MANIFEST.json, or galaxy.yml in a source checkout."""
    info: dict[str, object] | None = None
    if (raw := source.read("MANIFEST.json")) is not None:
        with contextlib.suppress(ValueError):
            info = _mapping(m.get("collection_info")) if (m := _mapping(json.loads(raw))) else None
    elif (raw := source.read("galaxy.yml")) is not None:
        info = _mapping(_safe_yaml(raw))
    info = info or {}
    dependencies = {k: v for k, v in (_mapping(info.get("dependencies")) or {}).items() if isinstance(v, str)}
    return _string(info.get("namespace")), _string(info.get("name")), _string(info.get("version")), dependencies


def _core_metadata(source: Source) -> _Metadata:
    """ansible.builtin, at the version in lib/ansible/release.py."""
    tree = _parse_python(source.read("lib/ansible/release.py") or b"")
    version = _string_assignments(tree.body)[0].get("__version__") if tree else None
    return "ansible", "builtin", version, {}


def _extract(source: Source, *, docs: bool, metadata: _Metadata | None = None) -> Collection:
    """Extract plugin, role and playbook entries, routing and, with `docs`, docs and fragments."""
    files = source.files()
    issues = _Issues()
    plugins: dict[tuple[str, str], str | None] = {}
    plugin_docs: dict[tuple[str, str], PluginDocs] = {}
    fragments: dict[str, dict[str, dict[str, object]]] = {}

    def add(ptype: str, name: str, candidates: _c.Iterable[_Raw]) -> None:
        # The first description found wins; a later one only fills a gap.
        # Docs work the same way.
        desc, found = _first_docs(candidates, docs, issues)
        if plugins.get((ptype, name)) is None:
            plugins[(ptype, name)] = desc
        if found is not None:
            _ = plugin_docs.setdefault((ptype, name), found)

    def parse(path: str) -> ast.Module | None:
        # A missing or unreadable file parses as an empty module.
        if (tree := _parse_python(source.read(path) or b"")) is None:
            issues.add("python_unparseable", path)
        return tree

    module_names: set[str] = set()
    for path in sorted(files):
        parts = path.split("/")

        if parts[0] == "plugins" and len(parts) >= 3:
            pdir, sub = parts[1], "/".join(parts[2:])
            stem, _, ext = sub.rpartition(".")
            if not stem or stem.endswith("__init__") or ext not in ("py", "ps1"):
                continue

            if pdir == "modules":
                # A module is often both <name>.ps1 and a <name>.py doc stub;
                # handle the pair once.
                name = _plugin_name(sub)
                if name in module_names:
                    continue
                module_names.add(name)
                py = f"plugins/modules/{stem}.py"
                tree = parse(py)
                add("module", name, _module_candidates(source, stem, _python_docs(py, tree, docs) if tree else None))

            elif pdir in ("filter", "test") and ext == "py":
                if (tree := parse(path)) is None or (names := _filter_test_names(tree, pdir)) is None:
                    issues.add("filter_test_unresolved", path)
                    continue
                prefix = sub.rpartition("/")[0].replace("/", ".")
                for n in names:
                    sidecars = (_sidecar_docs(source, f"plugins/{pdir}/{n}.{side}") for side in ("yml", "yaml"))
                    add(pdir, f"{prefix}.{n}" if prefix else n, sidecars)

            elif pdir == "doc_fragments" and ext == "py" and docs:
                attributes, unresolved = _fragment_attributes(tree) if (tree := parse(path)) else ({}, True)
                if unresolved:
                    issues.add("fragment_unresolved", path)
                fragment = {
                    attr: value
                    for attr, text in attributes.items()
                    if (value := _json_mapping(_safe_yaml(text), path, attr, issues)) is not None
                }
                if fragment:
                    fragments[_plugin_name(sub)] = fragment

            elif pdir in _PLUGIN_DIRS and ext == "py":
                tree = parse(path)
                add(_PLUGIN_DIRS[pdir], _plugin_name(sub), [_python_docs(path, tree, docs)] if tree else [])

        elif parts[0] == "roles" and len(parts) >= 3:
            role = parts[1]
            if ("role", role) in plugins:
                continue
            add("role", role, _role_candidates(source, role))

        elif parts[0] == "playbooks" and len(parts) == 2 and path.endswith((".yml", ".yaml")):
            # site.yml and site.yaml are the same playbook; playbooks have no docs.
            add("playbook", parts[1].rsplit(".", 1)[0], [])

    # Action-only plugins (no module of the same name) are invoked like modules.
    # Their docs are in the action plugin or a plugins/modules/<name>.yml sidecar.
    for path in sorted(files):
        if not path.startswith("plugins/action/") or not path.endswith(".py"):
            continue
        sub = path[len("plugins/action/") : -len(".py")]
        name = _plugin_name(f"{sub}.py")
        if name.endswith("__init__") or name in module_names:
            continue
        tree = parse(path)
        add("module", name, _module_candidates(source, sub, _python_docs(path, tree, docs) if tree else None))

    # Event-Driven Ansible: a name in more than one directory is only taken
    # from the first, which is the file ansible-rulebook loads.
    for directory, ptype, ext in _EDA_DIRS:
        for path in sorted(files):
            name = path[len(directory) : -len(ext)]
            if not path.startswith(directory) or not path.endswith(ext) or "/" in name or "." in name:
                continue
            if name == "__init__" or (ptype, name) in plugins:
                continue
            if ext == ".yml":
                add(ptype, name, [])  # rulebooks, like playbooks, have no docs
            else:
                tree = parse(path)
                add(ptype, name, [_python_docs(path, tree, docs)] if tree else [])

    routing: list[Routing] = []
    fragment_routing: dict[str, dict[str, object]] = {}
    runtime_path, runtime = _runtime(source, ("meta/runtime.yml", "meta/runtime.yaml"))
    types = {**_ROUTING_TYPES, "doc_fragments": "doc_fragments"} if docs else _ROUTING_TYPES
    for ptype, name, entry in _routing_entries(runtime, runtime_path, types, issues):
        if ptype == "doc_fragments":
            fragment_routing[name] = entry
        else:
            routing.append(Routing(type=ptype, name=name, entry=entry))
    eda_path, eda_runtime = _runtime(source, ("extensions/eda/eda_runtime.yml",))
    for ptype, name, entry in _routing_entries(eda_runtime, eda_path, _EDA_ROUTING_TYPES, issues):
        routing.append(Routing(type=ptype, name=name, entry=entry))

    namespace, collection, version, dependencies = metadata or _metadata(source)
    fqcn = f"{namespace}.{collection}" if namespace and collection else None
    return Collection(
        namespace=namespace,
        name=collection,
        version=version,
        dependencies=dependencies,
        requires_ansible=_string(runtime.get("requires_ansible")) if runtime else None,
        plugins=tuple(
            Plugin(type=t, name=n, short_description=d, docs=plugin_docs.get((t, n)))
            for (t, n), d in sorted(plugins.items())
        ),
        routing=tuple(sorted(routing, key=lambda r: (r.type, r.name))),
        fragments={
            name: DocFragment(collection=fqcn, name=name, attributes=attributes)
            for name, attributes in sorted(fragments.items())
        },
        fragment_routing=dict(sorted(fragment_routing.items())),
        issues=tuple(issues),
    )
