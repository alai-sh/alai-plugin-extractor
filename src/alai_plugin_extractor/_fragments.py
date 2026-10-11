"""Applying doc fragments the way ansible-core does when it loads a plugin's docs.

The merge and the collection tagging follow ansible-core's
lib/ansible/utils/plugin_docs.py (add_fragments, merge_fragment and
add_collection_to_versions_and_dates).
"""

import collections.abc as _c
import copy
import dataclasses
import typing as _t


@dataclasses.dataclass(frozen=True, slots=True, kw_only=True)
class DocFragment:
    """A doc fragment (plugins/doc_fragments/<name>.py) as the collection wrote it."""

    collection: str | None  # "ns.coll" defining it, None if the collection's name is unknown
    name: str  # within the collection, e.g. "auth", or "sub.auth" in a subdirectory
    # ModuleDocFragment's string attributes, parsed: DOCUMENTATION, RETURN and any
    # other uppercase name, which `ns.coll.auth.other` refers to.
    attributes: _c.Mapping[str, _c.Mapping[str, object]]


# Takes a fragment's full name (ns.coll.name) and returns it, or None if it
# isn't available.
FragmentLookup = _c.Callable[[str], DocFragment | None]

_SECTIONS = ("DOCUMENTATION", "RETURN")


def full_name(name: str) -> str:
    return name if name.count(".") >= 2 else f"ansible.builtin.{name}"


def _find(name: str, section: str, lookup: FragmentLookup) -> tuple[DocFragment, _c.Mapping[str, object]] | None:
    """The fragment `name` refers to, and the value it adds.

    `ns.coll.frag` is the fragment's DOCUMENTATION (or RETURN). If no fragment
    has the whole name, the last part names an attribute instead:
    `ns.coll.frag.facts` is the FACTS attribute of `ns.coll.frag`.
    """
    fragment = lookup(full_name(name))
    var = section
    if fragment is None and "." in name:
        base, _, suffix = name.rpartition(".")
        fragment, var = lookup(full_name(base)), suffix.upper()
    if fragment is None:
        return None
    value = fragment.attributes.get(var)
    if value is None and var in _SECTIONS:
        value = _t.cast(dict[str, object], {})  # e.g. a fragment without RETURN adds nothing to RETURN
    return (fragment, value) if value is not None else None


def apply_section(
    target: _c.Mapping[str, object] | None,
    names: tuple[str, ...],
    section: str,
    lookup: FragmentLookup,
    tag: tuple[str, bool] | None,
) -> tuple[_c.Mapping[str, object] | None, tuple[str, ...]]:
    """`target` (DOCUMENTATION or RETURN, per `section`) with the fragments
    `names` applied, and the names `lookup` couldn't supply. `tag` is the
    plugin's collection and whether it's a module, to label versions with."""
    if not names and (tag is None or target is None):
        return target, names
    returns = section == "RETURN"
    doc = copy.deepcopy(dict(target or {}))
    if tag:
        _label_versions(doc, tag[0], module=tag[1], returns=returns)
    missing: list[str] = []
    for name in names:
        if (found := _find(name, section, lookup)) is None:
            missing.append(name)
            continue
        fragment = copy.deepcopy(dict(found[1]))
        if tag and found[0].collection:
            _label_versions(fragment, found[0].collection, module=tag[1], returns=returns)
        if not returns:
            # A fragment's notes and seealso only ever extend the plugin's; an
            # empty one (community.general's _hpe3par has `notes:`) adds nothing.
            for key in ("notes", "seealso"):
                if isinstance(entries := fragment.pop(key, None), list) and entries:
                    current = doc.get(key)
                    doc[key] = [*_t.cast(list[object], current if isinstance(current, list) else []), *entries]
            for key in ("options", "attributes"):
                if isinstance(current := doc.get(key), dict) and isinstance(value := fragment.get(key), dict):
                    _merge(_t.cast(dict[str, object], current), _t.cast(dict[str, object], value))
                    del fragment[key]
        _merge(doc, fragment)
    return doc, tuple(missing)


def _merge(target: dict[str, object], source: dict[str, object]) -> None:
    """ansible-core's merge_fragment: `target`'s values win, dicts merge one level, lists combine."""
    for key, value in source.items():
        if key in target:
            current = target[key]
            if isinstance(current, dict) and isinstance(value, dict):
                value = {**_t.cast(dict[str, object], value), **_t.cast(dict[str, object], current)}
            elif isinstance(current, list) and isinstance(value, list):
                value = _combine(_t.cast(list[object], current), _t.cast(list[object], value))
            else:
                continue
        target[key] = value


def _combine(target: list[object], source: list[object]) -> list[object]:
    items: list[_t.Any] = source + target
    try:
        return sorted(set(items))
    except TypeError:
        # Dicts can't go in a set, and mixed types can't be sorted; ansible-core
        # fails on both. Keep every distinct item instead.
        return target + [item for item in source if item not in target]


def _dict_values(value: object) -> list[dict[str, object]]:
    """The dict values of `value`, if it's a dict (options, return values, attributes)."""
    values = _t.cast(dict[str, object], value).values() if isinstance(value, dict) else ()
    return [_t.cast(dict[str, object], v) for v in values if isinstance(v, dict)]


def _dict_items(value: object) -> list[dict[str, object]]:
    """The dict items of `value`, if it's a list (env, ini and vars specifiers)."""
    items = _t.cast(list[object], value) if isinstance(value, list) else []
    return [_t.cast(dict[str, object], v) for v in items if isinstance(v, dict)]


def _label_versions(doc: dict[str, object], collection: str, *, module: bool, returns: bool) -> None:
    """ansible-core's add_collection_to_versions_and_dates: name the collection
    each version and date belongs to, where the docs don't already."""

    def label(entry: dict[str, object], field: str) -> None:
        _ = entry.setdefault(field, collection)

    def deprecation(value: object, top_level: bool = False) -> None:
        if not isinstance(value, dict):
            return
        entry = _t.cast(dict[str, object], value)
        # Module deprecations use removed_in, plugin option deprecations version.
        if (
            ("removed_in" in entry and (module or top_level))
            or "removed_at_date" in entry
            or ("version" in entry and not (module or top_level))
        ):
            label(entry, "removed_from_collection" if top_level else "collection_name")

    def options(values: object) -> None:
        for option in _dict_values(values):
            if "version_added" in option:
                label(option, "version_added_collection")
            if not module:
                for key in ("env", "ini", "vars"):
                    for specifier in _dict_items(option.get(key)):
                        if "version_added" in specifier:
                            label(specifier, "version_added_collection")
                        deprecation(specifier.get("deprecated"))
                deprecation(option.get("deprecated"))
            options(option.get("suboptions"))

    def return_values(values: object) -> None:
        for value in _dict_values(values):
            if "version_added" in value:
                label(value, "version_added_collection")
            return_values(value.get("contains"))

    if returns:
        return_values(doc)
        return
    if "version_added" in doc:
        label(doc, "version_added_collection")
    deprecation(doc.get("deprecated"), top_level=True)
    options(doc.get("options"))
    for attribute in _dict_values(doc.get("attributes")):
        if "version_added" in attribute:
            label(attribute, "version_added_collection")
