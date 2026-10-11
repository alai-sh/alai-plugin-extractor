"""The result of an extraction, and fragment_lookup() over results.

The dataclasses are frozen, but the YAML read from a collection (docs,
routing entries, fragments) stays as the plain dicts and lists it was parsed
into. Every such value is JSON-safe: `json.dumps` writes it as standard JSON.
"""

import collections.abc as _c
import dataclasses
import typing as _t

from ._fragments import DocFragment, FragmentLookup, apply_section, full_name

# What extract() reads. More may be added; a str, not an enum, so it's easy to pass in.
SourceType = _t.Literal[
    "collection",  # a collection directory or tarball
    "ansible",  # an ansible-core source checkout, extracted as ansible.builtin
]

IssueKind = _t.Literal[
    "python_unparseable",  # a Python file that doesn't parse (often Python 2)
    "filter_test_unresolved",  # filter/test names that can't be found statically
    "fragment_unresolved",  # a doc fragment attribute that isn't a string constant
    "value_too_large",  # a YAML value past the size or depth limit, dropped
]


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Issue:
    """Something in the collection that extraction couldn't fully read."""

    kind: IssueKind
    path: str  # file within the collection
    detail: str = ""  # e.g. which value was too large


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class PluginDocs:
    """A plugin's docs as the collection wrote them, with no fragments applied.

    `extends_documentation_fragment` is taken out of DOCUMENTATION and RETURN
    and listed in `fragments` / `return_fragments`. Apply them with
    `apply_fragments()`, which leaves the names it couldn't resolve there.
    """

    documentation: _c.Mapping[str, object] | None = None  # DOCUMENTATION
    examples: str | None = None  # EXAMPLES, the source text
    returns: _c.Mapping[str, object] | None = None  # RETURN
    entry_points: _c.Mapping[str, object] | None = None  # a role's argument_specs, by entry point
    fragments: tuple[str, ...] = ()  # names to apply to `documentation`
    return_fragments: tuple[str, ...] = ()  # names to apply to `returns`

    def apply_fragments(
        self,
        lookup: FragmentLookup,
        *,
        collection: str | None = None,
        module: bool = False,
    ) -> _t.Self:
        """These docs with every fragment `lookup` can supply applied.

        `lookup` is always called with a full name. A short name such as
        `files` is ansible.builtin's, as in ansible-core. `fragment_lookup()`
        builds one from extracted collections. Names that `lookup` can't
        supply stay in `fragments` / `return_fragments`, so the result can be
        applied again once more collections are available.

        The merge follows ansible-core: the plugin's own values win, options
        and attributes merge per entry, `notes` and `seealso` are extended,
        and other lists are combined. Where ansible-core would fail (values of
        different types), the plugin's value is kept.

        With `collection`, the plugin's collection ("ns.coll"), every
        `version_added`, removal version and removal date is labelled with the
        collection it belongs to, as `ansible-doc` does
        (`version_added_collection` and so on): `collection` for the plugin's
        own docs, the fragment's collection for what a fragment adds. `module`
        must be True for modules, whose deprecations have a different shape.
        Without `collection`, nothing is labelled.
        """
        tag = (collection, module) if collection else None
        documentation, fragments = apply_section(self.documentation, self.fragments, "DOCUMENTATION", lookup, tag)
        returns, return_fragments = apply_section(self.returns, self.return_fragments, "RETURN", lookup, tag)
        return dataclasses.replace(
            self,
            documentation=documentation,
            returns=returns,
            fragments=fragments,
            return_fragments=return_fragments,
        )


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Plugin:
    """A module, plugin, filter, test, role or playbook."""

    type: str  # the `ansible-doc -t` name (module, lookup, filter, ...), role or playbook
    name: str  # short name; the full name is <namespace>.<name>.<name>
    short_description: str | None = None
    docs: PluginDocs | None = None  # only when extracted with docs=True, and the plugin has any


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Routing:
    """A `meta/runtime.yml` `plugin_routing` entry, which may name a plugin the collection no longer has."""

    type: str
    name: str
    entry: _c.Mapping[str, object]  # verbatim: redirect, deprecation, tombstone, ...


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class Collection:
    """Everything extracted from one collection."""

    # From MANIFEST.json, or galaxy.yml in a source checkout. None if missing or malformed.
    namespace: str | None
    name: str | None
    version: str | None
    dependencies: _c.Mapping[str, str]  # collection name -> version range
    requires_ansible: str | None  # from meta/runtime.yml
    plugins: tuple[Plugin, ...]  # sorted by (type, name)
    routing: tuple[Routing, ...]  # sorted by (type, name)
    # Doc fragments this collection defines, by name (docs=True only).
    fragments: _c.Mapping[str, DocFragment]
    # meta/runtime.yml doc_fragments routing, by fragment name (docs=True
    # only). Kept apart from `routing`, as fragments aren't plugins.
    fragment_routing: _c.Mapping[str, _c.Mapping[str, object]]
    issues: tuple[Issue, ...]


def fragment_lookup(
    collections: _c.Iterable[Collection],
) -> FragmentLookup:
    """A lookup over the fragments of `collections`, extracted with docs=True.

    It follows their doc_fragments routing as ansible-core does: routing is
    checked first, a redirect is followed (into any of `collections`), and a
    tombstone means the fragment was removed. ansible.builtin, extracted with
    type="ansible", redirects most of the short names Ansible 2.9 had
    (`azure` -> `azure.azcollection.azure`).
    """
    fragments: dict[str, DocFragment] = {}
    routing: dict[str, _c.Mapping[str, object]] = {}
    for c in collections:
        if c.namespace is None or c.name is None:
            continue  # its fragments can't be named
        prefix = f"{c.namespace}.{c.name}."
        fragments |= {prefix + name: fragment for name, fragment in c.fragments.items()}
        routing |= {prefix + name: entry for name, entry in c.fragment_routing.items()}

    def lookup(name: str) -> DocFragment | None:
        seen: set[str] = set()
        while name not in seen:
            seen.add(name)
            entry = routing.get(name, {})
            if "tombstone" in entry:
                return None
            if not isinstance(redirect := entry.get("redirect"), str):
                return fragments.get(name)
            name = full_name(redirect)
        return None  # a redirect loop

    return lookup
