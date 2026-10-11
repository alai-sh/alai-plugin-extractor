import copy

import alai_plugin_extractor as ext


class Lookup:
    """A fragment lookup over {full name: attributes} that records the names it was asked for."""

    def __init__(self, fragments):
        self.fragments = fragments
        self.calls = []

    def __call__(self, name):
        self.calls.append(name)
        if (attributes := self.fragments.get(name)) is None:
            return None
        namespace, collection, short = name.split(".", 2)
        return ext.DocFragment(collection=f"{namespace}.{collection}", name=short, attributes=attributes)


def test_merge_follows_ansible_core():
    docs = ext.PluginDocs(
        documentation={
            "short_description": "Thing",
            "author": "Plugin Author",
            "notes": ["Plugin note."],
            "requirements": ["b", "a"],
            "options": {
                "host": {"type": "str", "description": "Plugin's host."},
                "own": {"type": "int"},
            },
            "attributes": {"check_mode": {"support": "full"}},
        },
        fragments=("ns.other.auth",),
    )
    auth = {
        "DOCUMENTATION": {
            "author": "Fragment Author",
            "notes": ["Fragment note."],
            "seealso": [{"module": "ns.other.login"}],
            "requirements": ["a", "c"],
            "version_added": "1.0.0",
            "options": {
                "host": {"type": "str", "description": "Fragment's host.", "required": True},
                "token": {"type": "str", "no_log": True},
            },
            "attributes": {"check_mode": {"support": "none"}, "diff_mode": {"support": "none"}},
        }
    }
    merged = docs.apply_fragments(Lookup({"ns.other.auth": auth}))
    assert merged == ext.PluginDocs(
        documentation={
            "short_description": "Thing",
            # The plugin's own scalars win.
            "author": "Plugin Author",
            # notes and seealso are extended.
            "notes": ["Plugin note.", "Fragment note."],
            "seealso": [{"module": "ns.other.login"}],
            # Other lists are combined.
            "requirements": ["a", "b", "c"],
            # Keys only the fragment has are added.
            "version_added": "1.0.0",
            # Options and attributes merge per entry, the plugin's keys winning.
            "options": {
                "host": {"type": "str", "description": "Plugin's host.", "required": True},
                "own": {"type": "int"},
                "token": {"type": "str", "no_log": True},
            },
            "attributes": {"check_mode": {"support": "full"}, "diff_mode": {"support": "none"}},
        },
    )


def test_inputs_are_not_modified():
    docs = ext.PluginDocs(documentation={"options": {"a": {"type": "str"}}}, fragments=("ns.c.f",))
    fragments = {"ns.c.f": {"DOCUMENTATION": {"options": {"b": {"type": "int"}}, "notes": ["n"]}}}
    before = copy.deepcopy((docs, fragments))

    merged = docs.apply_fragments(Lookup(fragments))
    merged.documentation["options"]["b"]["type"] = "changed"
    merged.documentation["notes"].append("changed")
    assert (docs, fragments) == before


def test_names_resolve_like_ansible_core():
    docs = ext.PluginDocs(
        documentation={},
        fragments=(
            "files",  # a short name is ansible.builtin's
            "action_common_attributes.conn",  # .conn is the CONN attribute
            "ns.coll.frag",
            "ns.coll.frag.facts",
            "ns.coll.sub.name",  # a fragment in a subdirectory, not an attribute
        ),
    )
    lookup = Lookup(
        {
            "ansible.builtin.files": {"DOCUMENTATION": {"options": {"mode": {}}}},
            "ansible.builtin.action_common_attributes": {"CONN": {"attributes": {"connection": {}}}},
            "ns.coll.frag": {"DOCUMENTATION": {"options": {"a": {}}}, "FACTS": {"options": {"facts": {}}}},
            "ns.coll.sub.name": {"DOCUMENTATION": {"options": {"sub": {}}}},
        }
    )
    merged = docs.apply_fragments(lookup)
    assert merged.documentation == {
        "options": {"mode": {}, "a": {}, "facts": {}, "sub": {}},
        "attributes": {"connection": {}},
    }
    assert merged.fragments == ()
    assert lookup.calls == [
        "ansible.builtin.files",
        "ansible.builtin.action_common_attributes.conn",
        "ansible.builtin.action_common_attributes",
        "ns.coll.frag",
        "ns.coll.frag.facts",
        "ns.coll.frag",
        "ns.coll.sub.name",
    ]


def test_unresolved_names_stay_for_a_later_call():
    docs = ext.PluginDocs(
        documentation={"options": {"own": {}}},
        fragments=("ns.one.frag", "ns.two.frag", "ns.one.frag.missing_attribute", "nodots"),
    )
    one = {"ns.one.frag": {"DOCUMENTATION": {"options": {"one": {}}}}}
    first = docs.apply_fragments(Lookup(one))
    assert first.fragments == ("ns.two.frag", "ns.one.frag.missing_attribute", "nodots")
    assert first.documentation == {"options": {"own": {}, "one": {}}}

    two = {"ns.two.frag": {"DOCUMENTATION": {"options": {"two": {}}}}}
    second = first.apply_fragments(Lookup(two))
    assert second.fragments == ("ns.one.frag.missing_attribute", "nodots")
    assert second.documentation == {"options": {"own": {}, "one": {}, "two": {}}}


def test_return_fragments():
    docs = ext.PluginDocs(
        returns={"changed": {"type": "bool"}},
        return_fragments=("ns.c.results", "ns.c.no_return"),
    )
    fragments = {
        "ns.c.results": {"RETURN": {"stdout": {"type": "str"}}, "DOCUMENTATION": {"options": {"x": {}}}},
        # A fragment without RETURN adds nothing, as in ansible-core.
        "ns.c.no_return": {"DOCUMENTATION": {"options": {"y": {}}}},
    }
    merged = docs.apply_fragments(Lookup(fragments))
    assert merged == ext.PluginDocs(returns={"changed": {"type": "bool"}, "stdout": {"type": "str"}})


def test_nothing_to_apply_returns_the_same_docs():
    docs = ext.PluginDocs(documentation={"a": 1}, examples="- x: 1")
    lookup = Lookup({})
    assert docs.apply_fragments(lookup) == docs
    assert lookup.calls == []


def test_conflicts_ansible_core_rejects_keep_the_plugin_value():
    docs = ext.PluginDocs(
        documentation={
            "options": None,  # not a dict, so the fragment's options are dropped
            "notes": "a string",  # replaced by the fragment's list
            "seealso": [{"ref": "a"}],
            "items": [{"a": 1}, 2],  # dicts can't be sorted into a set
            "mixed": [1],
        },
        fragments=("ns.c.f",),
    )
    fragment = {
        "DOCUMENTATION": {
            "options": {"x": {}},
            "notes": ["Fragment note."],
            "seealso": "not a list",  # ignored, as ansible-core can't extend with it either
            "items": [{"a": 1}, {"b": 2}],
            "mixed": ["x"],  # ints and strings can't be sorted together
        }
    }
    merged = docs.apply_fragments(Lookup({"ns.c.f": fragment}))
    assert merged.documentation == {
        "options": None,
        "notes": ["Fragment note."],
        "seealso": [{"ref": "a"}],
        "items": [{"a": 1}, 2, {"b": 2}],
        "mixed": [1, "x"],
    }


def test_fragments_for_docs_without_documentation():
    # Only possible for PluginDocs built by the caller; the fragment becomes the docs.
    docs = ext.PluginDocs(fragments=("ns.c.f",))
    merged = docs.apply_fragments(Lookup({"ns.c.f": {"DOCUMENTATION": {"options": {"x": {}}}}}))
    assert merged.documentation == {"options": {"x": {}}}


def test_with_extracted_collections(tmp_path):
    def write(root, files):
        for name, content in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            _ = (root / name).write_text(content)

    write(
        tmp_path / "other",
        {
            "MANIFEST.json": '{"collection_info": {"namespace": "ns", "name": "other"}}',
            "plugins/doc_fragments/auth.py": 'class ModuleDocFragment:\n    DOCUMENTATION = "options: {token: {}}"\n',
        },
    )
    write(
        tmp_path / "coll",
        {
            "MANIFEST.json": '{"collection_info": {"namespace": "ns", "name": "coll"}}',
            "plugins/modules/m.py": 'DOCUMENTATION = """\nshort_description: M\nextends_documentation_fragment:\n'
            + '- ns.other.auth\n- ns.coll.common\n"""\n',
            "plugins/doc_fragments/common.py": 'class ModuleDocFragment:\n    DOCUMENTATION = "options: {x: {}}"\n',
        },
    )
    coll = ext.extract(tmp_path / "coll", docs=True)
    other = ext.extract(tmp_path / "other", docs=True)
    merged = coll.plugins[0].docs.apply_fragments(ext.fragment_lookup([coll, other]))
    assert merged == ext.PluginDocs(documentation={"short_description": "M", "options": {"token": {}, "x": {}}})


# -- fragment_lookup ------------------------------------------------------------


def collection(namespace, name, fragments, routing=None):
    return ext.Collection(
        namespace=namespace,
        name=name,
        version=None,
        dependencies={},
        requires_ansible=None,
        plugins=(),
        routing=(),
        fragments={
            n: ext.DocFragment(collection=f"{namespace}.{name}", name=n, attributes={"DOCUMENTATION": doc})
            for n, doc in fragments.items()
        },
        fragment_routing=routing or {},
        issues=(),
    )


def test_fragment_lookup_follows_routing():
    builtin = collection(
        "ansible",
        "builtin",
        {"files": {"options": {"mode": {}}}},
        {
            "azure": {"redirect": "azure.azcollection.azure"},
            "old": {"redirect": "ns.coll.moved"},
            "gone": {"tombstone": {"removal_version": "2.0.0"}},
            "loop_a": {"redirect": "ansible.builtin.loop_b"},
            "loop_b": {"redirect": "loop_a"},  # short names are ansible.builtin's here too
            "bad": {"redirect": ["not", "a", "name"]},
        },
    )
    azure = collection("azure", "azcollection", {"azure": {"options": {"auth": {}}}})
    coll = collection(
        "ns",
        "coll",
        {"real": {"options": {"real": {}}}, "files": {"options": {"shadowed": {}}}},
        {
            "moved": {"redirect": "ns.coll.real"},  # a chain: ansible.builtin.old -> ns.coll.moved -> ns.coll.real
            "files": {"redirect": "ansible.builtin.files"},  # routing wins over the file
            "warned": {"deprecation": {"removal_version": "3.0.0"}, "redirect": "ns.coll.real"},
        },
    )
    nameless = collection(None, None, {"orphan": {"options": {}}})
    lookup = ext.fragment_lookup([builtin, azure, coll, nameless])

    def options(name):
        fragment = lookup(name)
        return (fragment.collection, list(fragment.attributes["DOCUMENTATION"]["options"])) if fragment else None

    assert options("ansible.builtin.azure") == ("azure.azcollection", ["auth"])
    assert options("ansible.builtin.old") == ("ns.coll", ["real"])
    assert options("ns.coll.files") == ("ansible.builtin", ["mode"])
    assert options("ns.coll.warned") == ("ns.coll", ["real"])
    assert options("ansible.builtin.gone") is None
    assert options("ansible.builtin.loop_a") is None
    assert options("ansible.builtin.bad") is None
    assert options("ns.coll.missing") is None


def test_redirected_short_names_apply():
    # e.g. azure, which ansible.builtin redirects to azure.azcollection.azure.
    builtin = collection("ansible", "builtin", {}, {"azure": {"redirect": "azure.azcollection.azure"}})
    azure = collection("azure", "azcollection", {"azure": {"options": {"auth": {"version_added": "1.0.0"}}}})
    docs = ext.PluginDocs(documentation={"options": {}}, fragments=("azure",))
    merged = docs.apply_fragments(ext.fragment_lookup([builtin, azure]), collection="ns.coll", module=True)
    # The label is the collection the fragment really came from.
    assert merged.documentation == {
        "options": {"auth": {"version_added": "1.0.0", "version_added_collection": "azure.azcollection"}}
    }


# -- labelling versions with their collection -------------------------------------


def test_versions_are_labelled_for_a_plugin():
    docs = ext.PluginDocs(
        documentation={
            "version_added": "1.0.0",
            "deprecated": {"removed_at_date": "2030-01-01", "why": "old"},
            "options": {
                "a": {
                    "version_added": "1.1.0",
                    "env": [
                        {"name": "A", "version_added": "1.2.0", "deprecated": {"version": "3.0.0"}},
                        {"name": "B"},
                        "junk",
                    ],
                    "ini": "not a list",
                    "deprecated": {"version": "4.0.0"},
                    "suboptions": {"b": {"version_added": "1.3.0"}, "c": "junk"},
                },
                "untouched": {"type": "str"},
                "labelled": {"version_added": "0.1.0", "version_added_collection": "ns.elsewhere"},
                "junk": "not a dict",
            },
            "attributes": {"check_mode": {"version_added": "1.4.0"}, "diff_mode": {"support": "full"}},
        },
        returns={
            "out": {"version_added": "1.5.0", "contains": {"inner": {"version_added": "1.6.0"}}},
            "plain": {"type": "str"},
        },
    )
    labelled = docs.apply_fragments(Lookup({}), collection="ns.coll")
    assert labelled.documentation == {
        "version_added": "1.0.0",
        "version_added_collection": "ns.coll",
        "deprecated": {"removed_at_date": "2030-01-01", "why": "old", "removed_from_collection": "ns.coll"},
        "options": {
            "a": {
                "version_added": "1.1.0",
                "version_added_collection": "ns.coll",
                "env": [
                    {
                        "name": "A",
                        "version_added": "1.2.0",
                        "version_added_collection": "ns.coll",
                        "deprecated": {"version": "3.0.0", "collection_name": "ns.coll"},
                    },
                    {"name": "B"},
                    "junk",
                ],
                "ini": "not a list",
                "deprecated": {"version": "4.0.0", "collection_name": "ns.coll"},
                "suboptions": {"b": {"version_added": "1.3.0", "version_added_collection": "ns.coll"}, "c": "junk"},
            },
            "untouched": {"type": "str"},
            # An existing label is kept.
            "labelled": {"version_added": "0.1.0", "version_added_collection": "ns.elsewhere"},
            "junk": "not a dict",
        },
        "attributes": {
            "check_mode": {"version_added": "1.4.0", "version_added_collection": "ns.coll"},
            "diff_mode": {"support": "full"},
        },
    }
    assert labelled.returns == {
        "out": {
            "version_added": "1.5.0",
            "version_added_collection": "ns.coll",
            "contains": {"inner": {"version_added": "1.6.0", "version_added_collection": "ns.coll"}},
        },
        "plain": {"type": "str"},
    }
    # Unlabelled without a collection.
    assert docs.apply_fragments(Lookup({})) == docs


def test_module_deprecations_have_a_different_shape():
    doc = {
        "deprecated": {"removed_in": "3.0.0"},
        "options": {
            "a": {
                "env": [{"version_added": "1.0.0"}],  # modules don't read env, so it isn't labelled
                "deprecated": {"version": "3.0.0"},  # nor option deprecations
            }
        },
    }
    module = ext.PluginDocs(documentation=doc).apply_fragments(Lookup({}), collection="ns.coll", module=True)
    assert module.documentation == {
        "deprecated": {"removed_in": "3.0.0", "removed_from_collection": "ns.coll"},
        "options": {"a": {"env": [{"version_added": "1.0.0"}], "deprecated": {"version": "3.0.0"}}},
    }
    plugin = ext.PluginDocs(
        documentation={"deprecated": {"removed_in": "3.0.0"}, "options": {"a": {"deprecated": {"removed_in": "3.0.0"}}}}
    ).apply_fragments(Lookup({}), collection="ns.coll")
    # A plugin's top-level deprecation is labelled from removed_in, an option's isn't.
    assert plugin.documentation == {
        "deprecated": {"removed_in": "3.0.0", "removed_from_collection": "ns.coll"},
        "options": {"a": {"deprecated": {"removed_in": "3.0.0"}}},
    }


def test_fragment_versions_are_labelled_with_their_collection():
    docs = ext.PluginDocs(
        documentation={"options": {"both": {"version_added": "2.0.0"}, "own": {"version_added": "2.1.0"}}},
        returns={"rc": {"version_added": "2.2.0"}},
        fragments=("ns.other.frag", "ns.nameless.frag"),
        return_fragments=("ns.other.frag",),
    )
    other = ext.DocFragment(
        collection="ns.other",
        name="frag",
        attributes={
            "DOCUMENTATION": {"options": {"both": {"version_added": "1.0.0"}, "theirs": {"version_added": "1.1.0"}}},
            "RETURN": {"stdout": {"version_added": "1.2.0"}},
        },
    )
    nameless = ext.DocFragment(
        collection=None, name="frag", attributes={"DOCUMENTATION": {"options": {"anon": {"version_added": "1.3.0"}}}}
    )
    fragments = {"ns.other.frag": other, "ns.nameless.frag": nameless}
    merged = docs.apply_fragments(fragments.get, collection="ns.coll")
    assert merged.documentation == {
        "options": {
            # Labelled before merging, so the plugin's version keeps its own collection.
            "both": {"version_added": "2.0.0", "version_added_collection": "ns.coll"},
            "own": {"version_added": "2.1.0", "version_added_collection": "ns.coll"},
            "theirs": {"version_added": "1.1.0", "version_added_collection": "ns.other"},
            # A fragment from a collection with no known name isn't labelled.
            "anon": {"version_added": "1.3.0"},
        }
    }
    assert merged.returns == {
        "rc": {"version_added": "2.2.0", "version_added_collection": "ns.coll"},
        "stdout": {"version_added": "1.2.0", "version_added_collection": "ns.other"},
    }


def test_labelling_docs_without_returns_leaves_them_none():
    docs = ext.PluginDocs(examples="- x: 1")
    assert docs.apply_fragments(Lookup({}), collection="ns.coll") == docs


def test_empty_notes_in_a_fragment_add_nothing():
    # e.g. community.general's _hpe3par, whose DOCUMENTATION has `notes:` with no value.
    docs = ext.PluginDocs(documentation={"options": {}}, fragments=("ns.c.f",))
    fragment = {"DOCUMENTATION": {"options": {"x": {}}, "notes": None, "seealso": []}}
    assert docs.apply_fragments(Lookup({"ns.c.f": fragment})).documentation == {"options": {"x": {}}}
