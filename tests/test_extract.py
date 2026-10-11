import dataclasses
import io
import json
import tarfile

import pytest
import yaml

import alai_plugin_extractor as ext
from alai_plugin_extractor import _extract, _sources


def run(source, *, docs=False):
    return _extract._extract(source, docs=docs)


def routing(result):
    return {f"{r.type}:{r.name}": r.entry for r in result.routing}


def issues(result, kind):
    return [i.path for i in result.issues if i.kind == kind]


def doc_counts(result):
    """(found, missing) short descriptions, playbooks aside as they have no docs."""
    described = [p.short_description is not None for p in result.plugins if p.type != "playbook"]
    return sum(described), len(described) - sum(described)


def tar_source(data):
    return _sources.TarSource(tarfile.open(fileobj=io.BytesIO(data)))


class MemSource(_sources.Source):
    def __init__(self, files):
        self._files = {k: v.encode() if isinstance(v, str) else v for k, v in files.items()}

    def files(self):
        return set(self._files)

    def read(self, path):
        return self._files.get(path)


def names(result, ptype):
    return sorted(p.name for p in result.plugins if p.type == ptype)


def test_filters_from_local_variable_and_update():
    src = MemSource(
        {
            "plugins/filter/time.py": """
class FilterModule:
    def filters(self):
        filters = {"to_seconds": a, "to_minutes": b}
        filters.update({"to_hours": c})
        filters["to_days"] = d
        return filters
"""
        }
    )
    assert names(run(src), "filter") == ["to_days", "to_hours", "to_minutes", "to_seconds"]


def test_dict_call_and_direct_return():
    src = MemSource(
        {
            "plugins/filter/a.py": "class FilterModule:\n    def filters(self):\n        return dict(one=f, two=g)\n",
            "plugins/test/b.py": "class TestModule:\n    def tests(self):\n        return {'is_thing': t}\n",
        }
    )
    result = run(src)
    assert names(result, "filter") == ["one", "two"]
    assert names(result, "test") == ["is_thing"]


def test_dynamic_filters_are_reported_unresolved():
    src = MemSource(
        {
            "plugins/filter/dyn.py": "class FilterModule:\n    def filters(self):\n        return {k: v for k, v in X.items()}\n"
        }
    )
    result = run(src)
    assert names(result, "filter") == []
    assert issues(result, "filter_test_unresolved") == ["plugins/filter/dyn.py"]


def test_modules_docs_roles_playbooks_and_routing():
    src = MemSource(
        {
            "plugins/modules/thing.py": 'DOCUMENTATION = """\nmodule: thing\nshort_description: Manage   things\n"""\n',
            "plugins/modules/win_thing.ps1": "# powershell",
            "plugins/modules/win_thing.yml": "DOCUMENTATION:\n  short_description: Windows things\n",
            "plugins/modules/sub/nested.py": "",
            "plugins/action/only_action.py": "",
            "plugins/action/thing.py": "",
            "plugins/module_utils/helper.py": "",
            "roles/setup/tasks/main.yml": "",
            "roles/setup/meta/argument_specs.yml": "argument_specs:\n  main:\n    short_description: Set it up\n",
            "playbooks/site.yml": "",
            "meta/runtime.yml": "plugin_routing:\n  modules:\n    old_thing:\n      redirect: ns.coll.thing\n",
        }
    )
    result = run(src)
    plugins = {(p.type, p.name): p.short_description for p in result.plugins}
    assert plugins == {
        ("module", "thing"): "Manage things",
        ("module", "win_thing"): "Windows things",
        ("module", "sub.nested"): None,
        ("module", "only_action"): None,
        ("role", "setup"): "Set it up",
        ("playbook", "site"): None,
    }
    assert routing(result) == {"module:old_thing": {"redirect": "ns.coll.thing"}}


def test_python2_code_does_not_break_extraction():
    src = MemSource({"plugins/lookup/old.py": "print 'python 2'\n"})
    result = run(src)
    assert names(result, "lookup") == ["old"]
    assert len(issues(result, "python_unparseable")) == 1


def _tarball(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content, link in members:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type, info.linkname = tarfile.SYMTYPE, link
                tar.addfile(info)
            else:
                data = content.encode() if isinstance(content, str) else content
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_tar_source_follows_symlinks_and_ignores_traversal():
    manifest = json.dumps({"collection_info": {"namespace": "ns", "name": "coll", "version": "1.0.0"}})
    data = _tarball(
        [
            ("MANIFEST.json", manifest, None),
            ("plugins/modules/real.py", 'DOCUMENTATION = "short_description: Real"\n', None),
            ("plugins/modules/alias.py", None, "real.py"),
            ("../evil.py", "x", None),
        ]
    )
    src = tar_source(data)
    assert "../evil.py" not in src.files()
    result = run(src)
    assert (result.namespace, result.name, result.version) == ("ns", "coll", "1.0.0")
    assert {p.name: p.short_description for p in result.plugins} == {"alias": "Real", "real": "Real"}


def test_routing_dates_stay_strings():
    src = MemSource(
        {
            "meta/runtime.yml": "plugin_routing:\n  modules:\n    old:\n      deprecation:\n        removal_date: 2024-01-01\n"
        }
    )
    entries = routing(run(src))
    assert entries == {"module:old": {"deprecation": {"removal_date": "2024-01-01"}}}
    json.dumps(entries)


def test_class_attribute_returned_conditionally():
    src = MemSource(
        {
            "plugins/filter/ipv4.py": """
class FilterModule(object):
    filter_map = {"ipv4": _ipv4}

    def filters(self):
        if netaddr:
            return self.filter_map
        else:
            return dict((f, partial(_need_netaddr, f)) for f in self.filter_map)
"""
        }
    )
    assert names(run(src), "filter") == ["ipv4"]


def test_module_level_dict_returned_by_name():
    src = MemSource(
        {
            "plugins/test/t.py": "TESTS = {'a': x, 'b': y}\nclass TestModule:\n    def tests(self):\n        return TESTS\n"
        }
    )
    assert names(run(src), "test") == ["a", "b"]


def descriptions(result, ptype="module"):
    return {p.name: p.short_description for p in result.plugins if p.type == ptype}


# -- Python source edge cases -------------------------------------------------


def test_invalid_utf8_in_comment_still_parses():
    # Seen in h3c_bmc.hdm: a GBK comment in a file declaring UTF-8. Python 3.15
    # rejects it where 3.14 ignored invalid bytes in comments.
    src = MemSource(
        {
            "plugins/modules/redfish.py": b"# -*- coding: utf-8 -*-\n# Des: \xca\xca\xc5\xe4 G6\r\n"
            + b'DOCUMENTATION = "short_description: Redfish"\n',
        }
    )
    result = run(src)
    assert descriptions(result) == {"redfish": "Redfish"}
    assert len(issues(result, "python_unparseable")) == 0


def test_invalid_utf8_in_string_is_replaced():
    # A legacy-encoded byte in a string with no coding declaration fails on
    # every Python version, unlike one in a comment, which only 3.15 rejects.
    src = MemSource({"plugins/modules/legacy.py": b'DOCUMENTATION = "short_description: Caf\xe9"\n'})
    result = run(src)
    assert descriptions(result) == {"legacy": "Caf�"}
    assert len(issues(result, "python_unparseable")) == 0


def test_declared_source_encoding_is_honoured():
    src = MemSource(
        {
            "plugins/modules/latin.py": b"# -*- coding: latin-1 -*-\n"
            + b'DOCUMENTATION = "short_description: Caf\xe9"\n',
            "plugins/modules/bom.py": b'\xef\xbb\xbfDOCUMENTATION = "short_description: BOM"\n',
        }
    )
    assert descriptions(run(src)) == {"bom": "BOM", "latin": "Café"}


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(b"print 'python 2'\n", id="python2-print"),
        pytest.param(b"except Exception, e:\n", id="python2-except"),
        pytest.param(b'DOCUMENTATION = """\nshort_description: never closed\n', id="unterminated-string"),
        pytest.param(b"def f():\nreturn 1\n", id="bad-indentation"),
        pytest.param(b"x = 1\x00\n", id="null-byte"),
    ],
)
def test_unparseable_module_is_listed_without_description(source):
    result = run(MemSource({"plugins/modules/broken.py": source}))
    assert descriptions(result) == {"broken": None}
    assert len(issues(result, "python_unparseable")) == 1
    assert doc_counts(result)[1] == 1


# -- DOCUMENTATION and YAML ---------------------------------------------------


@pytest.mark.parametrize(
    ("doc", "expected"),
    [
        pytest.param("short_description: Plain", "Plain", id="plain"),
        pytest.param("short_description: >\n  Folded\n    across   lines\n", "Folded across lines", id="whitespace"),
        pytest.param("short_description: '   '", None, id="blank"),
        pytest.param("short_description: 42", None, id="not-a-string"),
        pytest.param("- short_description: In a list", None, id="list"),
        pytest.param("just a string", None, id="scalar"),
        pytest.param("short_description: [unclosed", None, id="invalid-yaml"),
        pytest.param("!!python/object/apply:os.system ['echo unsafe']", None, id="unsafe-tag"),
        pytest.param("short_description: !!python/name:os.system", None, id="unsafe-nested-tag"),
    ],
)
def test_module_documentation_shapes(doc, expected):
    src = MemSource({"plugins/modules/m.py": f"DOCUMENTATION = {doc!r}\n"})
    assert descriptions(run(src)) == {"m": expected}


@pytest.mark.parametrize(
    "source",
    [
        pytest.param('DOCUMENTATION = "short_description: " + "Joined"\n', id="concatenation"),
        pytest.param('def f():\n    DOCUMENTATION = "short_description: Local"\n', id="not-module-level"),
        pytest.param('DOCUMENTATION = b"short_description: Bytes"\n', id="bytes"),
        pytest.param('DOCUMENTATION = OTHER = "short_description: Chained"\n', id="chained-assignment"),
    ],
)
def test_documentation_must_be_a_module_level_string_constant(source):
    # Anything else would need evaluating the module, which extraction never does.
    assert descriptions(run(MemSource({"plugins/modules/m.py": source}))) == {"m": None}


@pytest.mark.parametrize(
    "sidecar",
    [
        pytest.param("DOCUMENTATION: [unclosed", id="invalid-yaml"),
        pytest.param("- DOCUMENTATION: {}", id="not-a-dict"),
        pytest.param("EXAMPLES: ''", id="no-documentation"),
    ],
)
def test_bad_sidecar_documentation_is_ignored(sidecar):
    src = MemSource({"plugins/modules/win.ps1": "", "plugins/modules/win.yml": sidecar})
    assert descriptions(run(src)) == {"win": None}


def test_yaml_sidecar_extension_and_python_doc_wins():
    src = MemSource(
        {
            "plugins/modules/both.py": 'DOCUMENTATION = "short_description: From Python"\n',
            "plugins/modules/both.yml": "DOCUMENTATION:\n  short_description: From sidecar\n",
            "plugins/modules/side.ps1": "",
            "plugins/modules/side.yaml": "DOCUMENTATION:\n  short_description: From .yaml\n",
        }
    )
    assert descriptions(run(src)) == {"both": "From Python", "side": "From .yaml"}


# -- collection layout --------------------------------------------------------


def test_files_that_are_not_plugins_are_ignored():
    src = MemSource(
        {
            "plugins/modules/__init__.py": "",
            "plugins/modules/sub/__init__.py": "",
            "plugins/modules/README.md": "",
            "plugins/modules/notes.txt": "",
            "plugins/modules/a/b/deep.py": "",
            "plugins/action/__init__.py": "",
            "plugins/doc_fragments/frag.py": "",
            "plugins/module_utils/util.py": "",
            "plugins/lookup/data.json": "",
            "plugins/lookup/__init__.py": "",
            "plugins/lookup/real.py": "",
            "playbooks/nested/deep.yml": "",
            "playbooks/notes.md": "",
            "playbooks/site.yaml": "",
            "docs/plugins/modules/fake.py": "",
        }
    )
    plugins = sorted((p.type, p.name) for p in run(src).plugins)
    assert plugins == [("lookup", "real"), ("module", "a.b.deep"), ("playbook", "site")]


def test_filters_in_subdirectories_are_prefixed_and_use_sidecars():
    src = MemSource(
        {
            "plugins/filter/net/ip.py": "class FilterModule:\n    def filters(self):\n        return {'ipaddr': f}\n",
            "plugins/filter/ipaddr.yml": "DOCUMENTATION:\n  short_description: IP addresses\n",
        }
    )
    assert descriptions(run(src), "filter") == {"net.ipaddr": "IP addresses"}


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("return dict(**OTHER)", id="dict-kwargs-spread"),
        pytest.param("return {**OTHER, 'a': f}", id="dict-spread"),
        pytest.param("return {name(): f}", id="computed-key"),
        pytest.param("return dict(OTHER)", id="dict-positional"),
        pytest.param("return build()", id="call"),
    ],
)
def test_unresolvable_filter_names(body):
    src = MemSource({"plugins/filter/f.py": f"class FilterModule:\n    def filters(self):\n        {body}\n"})
    result = run(src)
    assert names(result, "filter") == []
    assert issues(result, "filter_test_unresolved") == ["plugins/filter/f.py"]


def test_filter_file_without_filter_module_class_is_unresolved():
    src = MemSource({"plugins/filter/f.py": "class Filters:\n    def filters(self):\n        return {'a': f}\n"})
    assert issues(run(src), "filter_test_unresolved") == ["plugins/filter/f.py"]


def test_filter_returns_inside_try_and_for_are_merged():
    src = MemSource(
        {
            "plugins/filter/f.py": """
class FilterModule:
    def filters(self):
        try:
            import thing
        except ImportError:
            return {"fallback": f}
        else:
            for _ in range(1):
                return {"primary": g}
"""
        }
    )
    assert names(run(src), "filter") == ["fallback", "primary"]


def test_role_descriptions():
    src = MemSource(
        {
            "roles/main_meta/tasks/main.yml": "",
            "roles/main_meta/meta/main.yml": "argument_specs:\n  main:\n    short_description: From main.yml\n",
            "roles/no_meta/tasks/main.yml": "",
            "roles/bad_meta/meta/argument_specs.yml": "argument_specs: [not, a, dict]\n",
            "roles/no_main/meta/argument_specs.yml": "argument_specs:\n  other:\n    short_description: Not main\n",
            "roles/shallow.yml": "",
        }
    )
    assert descriptions(run(src), "role") == {
        "bad_meta": None,
        "main_meta": "From main.yml",
        "no_main": None,
        "no_meta": None,
    }


# -- collection metadata ----------------------------------------------------------


MANIFEST = {
    "collection_info": {
        "namespace": "ns",
        "name": "coll",
        "version": "1.0.0",
        "dependencies": {"ns.other": ">=1.0.0", "ns.bad": 1},
    }
}


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        pytest.param(
            {"MANIFEST.json": json.dumps(MANIFEST)}, ("ns", "coll", "1.0.0", {"ns.other": ">=1.0.0"}), id="manifest"
        ),
        pytest.param({"MANIFEST.json": b"{not json"}, (None, None, None, {}), id="invalid-json"),
        pytest.param({"MANIFEST.json": b"\xff\xfe"}, (None, None, None, {}), id="not-utf8"),
        pytest.param({"MANIFEST.json": b"[1, 2]"}, (None, None, None, {}), id="not-an-object"),
        pytest.param(
            {"MANIFEST.json": b'{"collection_info": "ns.coll"}'}, (None, None, None, {}), id="info-not-an-object"
        ),
        pytest.param(
            {"MANIFEST.json": b'{"collection_info": {"name": 1, "dependencies": []}}'},
            (None, None, None, {}),
            id="wrong-types",
        ),
        pytest.param(
            {"galaxy.yml": "namespace: ns\nname: src\nversion: 2.0.0\ndependencies: {ns.other: '*'}\n"},
            ("ns", "src", "2.0.0", {"ns.other": "*"}),
            id="source-checkout",
        ),
        pytest.param(
            {"MANIFEST.json": b"{}", "galaxy.yml": "namespace: ns\n"}, (None, None, None, {}), id="manifest-wins"
        ),
        pytest.param({}, (None, None, None, {}), id="neither"),
    ],
)
def test_collection_metadata(files, expected):
    result = run(MemSource(files))
    assert (result.namespace, result.name, result.version, result.dependencies) == expected


@pytest.mark.parametrize(
    ("runtime", "expected"),
    [
        pytest.param("requires_ansible: '>=2.16'\n", ">=2.16", id="set"),
        pytest.param("requires_ansible: 2.16\n", None, id="not-a-string"),
        pytest.param("plugin_routing: {}\n", None, id="missing"),
    ],
)
def test_requires_ansible(runtime, expected):
    assert run(MemSource({"meta/runtime.yml": runtime})).requires_ansible == expected


# -- meta/runtime.yml -----------------------------------------------------------


@pytest.mark.parametrize(
    "runtime",
    [
        pytest.param("plugin_routing: [a, b]\n", id="routing-not-a-dict"),
        pytest.param("plugin_routing:\n  modules: [a]\n", id="entries-not-a-dict"),
        pytest.param("plugin_routing:\n  modules:\n    a: redirect\n", id="entry-not-a-dict"),
        pytest.param("plugin_routing:\n  modules:\n    a: {}\n", id="empty-entry"),
        pytest.param("plugin_routing:\n  doc_fragments:\n    a: {redirect: x.y.z}\n", id="unindexed-type"),
        pytest.param("plugin_routing: {modules: [unclosed\n", id="invalid-yaml"),
        pytest.param("- not a mapping\n", id="not-a-mapping"),
    ],
)
def test_malformed_runtime_routing_is_skipped(runtime):
    assert routing(run(MemSource({"meta/runtime.yml": runtime}))) == {}


def test_runtime_yaml_extension_and_routing_types():
    runtime = """
plugin_routing:
  modules:
    old_mod: {redirect: ns.coll.new_mod}
  filter:
    old_filter: {tombstone: {removal_version: 2.0.0}}
  lookup:
    old_lookup: {deprecation: {removal_date: 2025-06-01}}
"""
    entries = routing(run(MemSource({"meta/runtime.yaml": runtime})))
    assert entries == {
        "module:old_mod": {"redirect": "ns.coll.new_mod"},
        "filter:old_filter": {"tombstone": {"removal_version": "2.0.0"}},
        "lookup:old_lookup": {"deprecation": {"removal_date": "2025-06-01"}},
    }


# -- sources --------------------------------------------------------------------


def test_tar_source_edge_cases(monkeypatch):
    monkeypatch.setattr(_sources, "_MAX_MEMBER", 64)
    data = _tarball(
        [
            ("./plugins/modules/dot.py", 'DOCUMENTATION = "short_description: Dot"\n', None),
            ("/plugins/modules/absolute.py", 'DOCUMENTATION = "short_description: Abs"\n', None),
            ("plugins/modules/huge.py", "x = 1\n" * 20, None),
            ("plugins/modules/dangling.py", None, "missing.py"),
            ("plugins/modules/loop_a.py", None, "loop_b.py"),
            ("plugins/modules/loop_b.py", None, "loop_a.py"),
            ("plugins/modules/escape.py", None, "../../../../etc/passwd"),
            ("plugins/modules/../../../outside.py", "x", None),
            ("plugins/../plugins/modules/dotdot.py", "", None),
        ]
    )
    src = tar_source(data)
    assert src.files() == {
        "plugins/modules/dot.py",
        "plugins/modules/absolute.py",
        "plugins/modules/huge.py",
        "plugins/modules/dangling.py",
        "plugins/modules/loop_a.py",
        "plugins/modules/loop_b.py",
        "plugins/modules/escape.py",
        "plugins/modules/dotdot.py",
    }
    for unreadable in ("huge.py", "dangling.py", "loop_a.py", "escape.py"):
        assert src.read(f"plugins/modules/{unreadable}") is None
    assert descriptions(run(src)) == {
        "absolute": "Abs",
        "dangling": None,
        "dot": "Dot",
        "dotdot": None,
        "escape": None,
        "huge": None,
        "loop_a": None,
        "loop_b": None,
    }


def test_tar_source_ignores_directories_and_hardlinks():
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        directory = tarfile.TarInfo("plugins/modules/dir.py")
        directory.type = tarfile.DIRTYPE
        tar.addfile(directory)
        link = tarfile.TarInfo("plugins/modules/hard.py")
        link.type, link.linkname = tarfile.LNKTYPE, "plugins/modules/dir.py"
        tar.addfile(link)
    assert tar_source(buf.getvalue()).files() == set()


def test_dir_source_stays_inside_root(tmp_path):
    root = tmp_path / "coll"
    modules = root / "plugins" / "modules"
    modules.mkdir(parents=True)
    (modules / "real.py").write_text('DOCUMENTATION = "short_description: Real"\n')
    (modules / "alias.py").symlink_to("real.py")
    (modules / "dangling.py").symlink_to("missing.py")
    (tmp_path / "secret.py").write_text('DOCUMENTATION = "short_description: Secret"\n')
    (modules / "escape.py").symlink_to(tmp_path / "secret.py")

    src = _sources.DirSource(root)
    assert "plugins/modules/dangling.py" not in src.files()
    assert src.read("../secret.py") is None
    assert src.read("plugins/modules/escape.py") is None
    assert descriptions(run(src)) == {"alias": "Real", "escape": None, "real": "Real"}
    # The public entry point takes any os.PathLike or a str.
    assert descriptions(ext.extract(str(root))) == descriptions(run(src))


def test_ansible_core_source_layout():
    src = _sources.AnsibleCoreSource(
        MemSource(
            {
                "lib/ansible/modules/ping.py": 'DOCUMENTATION = "short_description: Ping"\n',
                "lib/ansible/plugins/filter/core.py": "class FilterModule:\n    def filters(self):\n        return {'b64': f}\n",
                "lib/ansible/plugins/lookup/env.py": "",
                "lib/ansible/config/ansible_builtin_runtime.yml": "plugin_routing:\n  modules:\n    docker: {redirect: community.docker.docker_container}\n",
                "lib/ansible/cli/doc.py": "",
                "test/units/modules/test_ping.py": "",
            }
        )
    )
    assert src.read("lib/ansible/cli/doc.py") is None
    result = run(src)
    assert sorted((p.type, p.name) for p in result.plugins) == [
        ("filter", "b64"),
        ("lookup", "env"),
        ("module", "ping"),
    ]
    assert routing(result) == {"module:docker": {"redirect": "community.docker.docker_container"}}


# -- filter name resolution: mutations --------------------------------------------


def _filters(body):
    lines = "\n".join(f"        {line}" for line in body.strip().splitlines())
    return MemSource({"plugins/filter/f.py": f"class FilterModule:\n    def filters(self):\n{lines}\n"})


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param("x = {'a': f}\nx['b'] = g\nreturn x", ["a", "b"], id="literal-subscript"),
        pytest.param("x = {'a': f}\nx |= {'b': g}\nreturn x", ["a", "b"], id="or-assign"),
        pytest.param("x = {'a': f}\nx.update(b=g, c=h)\nreturn x", ["a", "b", "c"], id="update-keywords"),
        pytest.param("x = {'a': f}\nx.update({'b': g}, c=h)\nreturn x", ["a", "b", "c"], id="update-mixed"),
        pytest.param("self.x = {'a': f}\nself.x['b'] = g\nreturn self.x", ["a", "b"], id="self-attribute"),
        pytest.param("x = {'a': f}\nother.attr[NAME] = 1\nreturn x", ["a"], id="unrelated-subscript"),
    ],
)
def test_filter_dict_mutations_that_resolve(body, expected):
    assert names(run(_filters(body)), "filter") == expected


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("x = {'a': f}\nx[NAME] = g\nreturn x", id="computed-subscript"),
        pytest.param("x = {'a': f}\nx[1] = g\nreturn x", id="non-string-subscript"),
        pytest.param("self.x = {'a': f}\nself.x[NAME] = g\nreturn self.x", id="computed-self-subscript"),
        pytest.param("x = {'a': f}\nx |= OTHER\nreturn x", id="or-assign-unknown"),
        pytest.param("x = {'a': f}\nx += OTHER\nreturn x", id="other-augmented-op"),
        pytest.param("x = {'a': f}\nx.update(**OTHER)\nreturn x", id="update-kwargs-spread"),
        pytest.param("x = {'a': f}\nx.update(OTHER)\nreturn x", id="update-unknown"),
        pytest.param("x = {'a': f}\nx.update(A, B)\nreturn x", id="update-two-args"),
    ],
)
def test_filter_dict_mutations_that_cannot_resolve(body):
    # A name the extractor can't see must make the file unresolved, never be
    # silently dropped from an otherwise resolved list.
    result = run(_filters(body))
    assert names(result, "filter") == []
    assert issues(result, "filter_test_unresolved") == ["plugins/filter/f.py"]


def test_filter_module_without_filters_method_is_unresolved():
    src = MemSource({"plugins/filter/f.py": "class FilterModule:\n    def other(self):\n        return {'a': f}\n"})
    assert issues(run(src), "filter_test_unresolved") == ["plugins/filter/f.py"]


def test_unparseable_filter_file_is_unresolved():
    result = run(MemSource({"plugins/filter/f.py": "print 'python 2'\n"}))
    assert names(result, "filter") == []
    assert len(issues(result, "python_unparseable")) == 1
    assert issues(result, "filter_test_unresolved") == ["plugins/filter/f.py"]


# -- modules backed by PowerShell or action plugins -------------------------------


def test_powershell_module_with_python_doc_stub_is_counted_once():
    src = MemSource(
        {
            "plugins/modules/win_thing.ps1": "# implementation",
            "plugins/modules/win_thing.py": 'DOCUMENTATION = "short_description: Windows thing"\n',
            "plugins/modules/win_broken.ps1": "# implementation",
            "plugins/modules/win_broken.py": "print 'python 2'\n",
        }
    )
    result = run(src)
    assert descriptions(result) == {"win_broken": None, "win_thing": "Windows thing"}
    assert len(issues(result, "python_unparseable")) == 1
    assert doc_counts(result) == (1, 1)


def test_action_only_module_docs_from_module_sidecar():
    # e.g. capable.core212: the implementation is an action plugin and the docs
    # are only a plugins/modules/<name>.yml sidecar.
    src = MemSource(
        {
            "plugins/action/show.py": "class ActionModule: pass\n",
            "plugins/modules/show.yml": "DOCUMENTATION:\n  short_description: From sidecar\n",
            "plugins/action/net/ping.py": "",
            "plugins/modules/net/ping.yaml": "DOCUMENTATION:\n  short_description: Nested sidecar\n",
            "plugins/action/own.py": 'DOCUMENTATION = "short_description: From action"\n',
            "plugins/modules/own.yml": "DOCUMENTATION:\n  short_description: From sidecar\n",
            "plugins/action/undocumented.py": "",
            "plugins/action/broken.py": "print 'python 2'\n",
            "plugins/modules/broken.yml": "DOCUMENTATION:\n  short_description: Still documented\n",
        }
    )
    result = run(src)
    assert descriptions(result) == {
        "broken": "Still documented",
        "net.ping": "Nested sidecar",
        "own": "From action",
        "show": "From sidecar",
        "undocumented": None,
    }
    assert len(issues(result, "python_unparseable")) == 1
    # The sidecar alone isn't a module, so nothing is listed twice.
    assert doc_counts(result) == (4, 1)


def test_action_with_module_uses_module_docs():
    src = MemSource(
        {
            "plugins/action/win_copy.py": 'DOCUMENTATION = "short_description: Action"\n',
            "plugins/modules/win_copy.ps1": "",
            "plugins/modules/win_copy.py": 'DOCUMENTATION = "short_description: Module"\n',
        }
    )
    assert descriptions(run(src)) == {"win_copy": "Module"}


def test_playbook_with_both_extensions_is_listed_once():
    src = MemSource({"playbooks/site.yml": "", "playbooks/site.yaml": ""})
    result = run(src)
    assert [(p.type, p.name) for p in result.plugins] == [("playbook", "site")]
    assert doc_counts(result) == (0, 0)


def test_filter_defined_in_two_files_is_listed_once():
    body = "class FilterModule:\n    def filters(self):\n        return {'shared': f}\n"
    src = MemSource(
        {
            "plugins/filter/a.py": body,
            "plugins/filter/b.py": body,
            "plugins/filter/shared.yml": "DOCUMENTATION:\n  short_description: Shared\n",
        }
    )
    result = run(src)
    assert descriptions(result, "filter") == {"shared": "Shared"}
    assert doc_counts(result) == (1, 0)


# -- public entry points ------------------------------------------------------------

COLLECTION = {
    "MANIFEST.json": json.dumps({"collection_info": {"namespace": "ns", "name": "coll", "version": "1.0.0"}}),
    "plugins/modules/thing.py": 'DOCUMENTATION = "short_description: Thing"\n',
}


def test_extract_directory_and_tarball_path(tmp_path):
    root = tmp_path / "coll"
    for name, content in COLLECTION.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        _ = (root / name).write_text(content)
    tarball = tmp_path / "ns-coll-1.0.0.tar.gz"
    _ = tarball.write_bytes(_tarball([(n, c, None) for n, c in COLLECTION.items()]))

    from_dir = ext.extract(root)
    from_tar = ext.extract(str(tarball))
    assert from_dir == from_tar
    assert (from_dir.namespace, from_dir.name, from_dir.version) == ("ns", "coll", "1.0.0")
    assert from_dir.plugins == (ext.Plugin(type="module", name="thing", short_description="Thing"),)


@pytest.mark.parametrize("wrap", [bytes, bytearray, memoryview])
def test_extract_bytes(wrap):
    result = ext.extract_bytes(wrap(_tarball([(n, c, None) for n, c in COLLECTION.items()])), docs=True)
    assert result.plugins[0].docs == ext.PluginDocs(documentation={"short_description": "Thing"})


def test_extract_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        _ = ext.extract(tmp_path / "missing")
    notes = tmp_path / "notes.txt"
    _ = notes.write_text("not a tarball")
    with pytest.raises(tarfile.TarError):
        _ = ext.extract(notes)
    with pytest.raises(tarfile.TarError):
        _ = ext.extract_bytes(b"not a tarball")
    with pytest.raises(ValueError, match="type='ansible' needs a directory"):
        _ = ext.extract(notes, type="ansible")
    with pytest.raises(ValueError, match="type='ansible' needs a directory"):
        _ = ext.extract_bytes(b"", type="ansible")
    # A type from a newer release mustn't be read as a collection.
    for call in (ext.extract, ext.extract_bytes):
        with pytest.raises(ValueError, match="unknown type 'legacy_role', expected one of: collection, ansible"):
            _ = call(tmp_path, type="legacy_role")


def test_extract_builtin(tmp_path):
    files = {
        "lib/ansible/release.py": "__version__ = '2.20.0'\n__author__ = 'Ansible'\n",
        "lib/ansible/modules/ping.py": 'DOCUMENTATION = "short_description: Ping"\n',
        "lib/ansible/plugins/doc_fragments/files.py": 'class ModuleDocFragment:\n    DOCUMENTATION = "options: {}"\n',
    }
    for name, content in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        _ = (tmp_path / name).write_text(content)
    result = ext.extract(tmp_path, type="ansible", docs=True)
    assert (result.namespace, result.name, result.version, result.dependencies) == ("ansible", "builtin", "2.20.0", {})
    assert [(p.type, p.name) for p in result.plugins] == [("module", "ping")]
    assert result.fragments == {
        "files": ext.DocFragment(
            collection="ansible.builtin", name="files", attributes={"DOCUMENTATION": {"options": {}}}
        )
    }

    (tmp_path / "lib/ansible/release.py").unlink()
    assert ext.extract(tmp_path, type="ansible").version is None


# -- full docs ------------------------------------------------------------------


def plugin_docs(result, ptype="module"):
    return {p.name: p.docs for p in result.plugins if p.type == ptype and p.docs is not None}


def test_docs_are_off_by_default():
    result = run(
        MemSource(
            {
                "plugins/modules/m.py": 'DOCUMENTATION = "short_description: M"\n',
                "plugins/doc_fragments/f.py": 'class ModuleDocFragment:\n    DOCUMENTATION = "options: {}"\n',
            }
        )
    )
    assert result.plugins == (ext.Plugin(type="module", name="m", short_description="M"),)
    assert result.fragments == {}


def test_module_docs_are_raw():
    module = '''
DOCUMENTATION = """
short_description: Thing
extends_documentation_fragment:
- ns.other.auth
- ns.coll.common.facts
options:
  name: {type: str}
"""
EXAMPLES = """
- ns.coll.thing:
    name: x
"""
RETURN = """
extends_documentation_fragment: ns.other.results, files
changed: {type: bool}
"""
'''
    src = MemSource({"plugins/modules/thing.py": module})
    result = run(src, docs=True)
    assert plugin_docs(result) == {
        # Fragments are listed for the caller, who may have ns.other.
        "thing": ext.PluginDocs(
            documentation={"short_description": "Thing", "options": {"name": {"type": "str"}}},
            examples="\n- ns.coll.thing:\n    name: x\n",
            returns={"changed": {"type": "bool"}},
            fragments=("ns.other.auth", "ns.coll.common.facts"),
            return_fragments=("ns.other.results", "files"),
        )
    }
    # Asking for docs doesn't change anything else.
    assert dataclasses.replace(result, plugins=tuple(dataclasses.replace(p, docs=None) for p in result.plugins)) == run(
        src
    )


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        pytest.param('EXAMPLES = "  "\nRETURN = ""\n', None, id="blank-examples-empty-return"),
        pytest.param('RETURN = "- a list"\n', None, id="return-not-a-mapping"),
        pytest.param('EXAMPLES = "- x: 1"\n', ext.PluginDocs(examples="- x: 1"), id="examples-only"),
        pytest.param("DOCUMENTATION: str\n", None, id="annotation-without-value"),
        # e.g. jomrr.ca's fragments
        pytest.param('DOCUMENTATION: str = "a: 1"\n', ext.PluginDocs(documentation={"a": 1}), id="annotated"),
        pytest.param('x.DOCUMENTATION = "a: 1"\n', None, id="attribute-target"),
        pytest.param(
            'DOCUMENTATION = "a: 1"\nDOCUMENTATION = "a: 2"\n',
            ext.PluginDocs(documentation={"a": 1}),
            id="first-constant-wins",
        ),
        pytest.param(
            'DOCUMENTATION = build()\nDOCUMENTATION = "a: 2"\n',
            ext.PluginDocs(documentation={"a": 2}),
            id="constant-after-call",
        ),
        pytest.param(
            "DOCUMENTATION = \"extends_documentation_fragment: [a, 1, ' ', {b: c}]\"\n",
            ext.PluginDocs(documentation={}, fragments=("a",)),
            id="fragment-list-junk",
        ),
        pytest.param(
            'DOCUMENTATION = "extends_documentation_fragment: {a: b}"\n',
            ext.PluginDocs(documentation={}),
            id="fragments-not-a-list",
        ),
    ],
)
def test_module_doc_variants(module, expected):
    assert plugin_docs(run(MemSource({"plugins/modules/m.py": module}), docs=True)).get("m") == expected


def test_doc_sources_in_priority_order():
    src = MemSource(
        {
            # Python docs win, and the sidecar isn't needed.
            "plugins/modules/both.py": 'DOCUMENTATION = "short_description: Py"\n',
            "plugins/modules/both.yml": "DOCUMENTATION:\n  short_description: Sidecar\n",
            # Docs without a short_description: the description comes from the
            # sidecar, the docs from the first source that has any.
            "plugins/modules/split.py": 'DOCUMENTATION = "options: {}"\n',
            "plugins/modules/split.yml": "DOCUMENTATION:\n  short_description: Sidecar\n",
            # A .ps1 module documented only by a sidecar.
            "plugins/modules/win.ps1": "",
            "plugins/modules/win.yml": "DOCUMENTATION:\n  short_description: Win\nEXAMPLES: '- win: {}'\n"
            + "RETURN:\n  out: {type: str}\n",
            # Action-only, documented by a .yaml sidecar.
            "plugins/action/act.py": "",
            "plugins/modules/act.yaml": "DOCUMENTATION:\n  short_description: Act\n",
            # Nothing usable anywhere.
            "plugins/modules/none.py": "x = 1\n",
            "plugins/modules/none.yml": "- not a mapping\n",
        }
    )
    result = run(src, docs=True)
    assert descriptions(result) == {"act": "Act", "both": "Py", "none": None, "split": "Sidecar", "win": "Win"}
    assert plugin_docs(result) == {
        "act": ext.PluginDocs(documentation={"short_description": "Act"}),
        "both": ext.PluginDocs(documentation={"short_description": "Py"}),
        "split": ext.PluginDocs(documentation={"options": {}}),
        "win": ext.PluginDocs(
            documentation={"short_description": "Win"}, examples="- win: {}", returns={"out": {"type": "str"}}
        ),
    }


def test_sidecar_is_not_read_when_python_has_everything():
    class Counting(MemSource):
        def __init__(self, files):
            super().__init__(files)
            self.reads = []

        def read(self, path):
            self.reads.append(path)
            return super().read(path)

    src = Counting({"plugins/modules/m.py": 'DOCUMENTATION = "short_description: M"\n'})
    _ = run(src)
    _ = run(src, docs=True)
    assert [p for p in src.reads if p.startswith("plugins/modules/m.")] == ["plugins/modules/m.py"] * 2


def test_docs_for_filters_roles_and_other_plugins():
    src = MemSource(
        {
            "plugins/filter/f.py": "class FilterModule:\n    def filters(self):\n        return {'a': x, 'b': y}\n",
            "plugins/filter/a.yml": "DOCUMENTATION:\n  short_description: A\nEXAMPLES: 'x | ns.coll.a'\n",
            "plugins/lookup/l.py": 'DOCUMENTATION = "short_description: L"\nRETURN = "_raw: {}"\n',
            "plugins/lookup/broken.py": "print 'python 2'\n",
            "roles/r/meta/argument_specs.yml": "argument_specs:\n  main: {short_description: Main}\n"
            + "  other: {options: {}}\n",
            "playbooks/site.yml": "",
        }
    )
    result = run(src, docs=True)
    assert {(p.type, p.name): p.docs for p in result.plugins} == {
        ("filter", "a"): ext.PluginDocs(documentation={"short_description": "A"}, examples="x | ns.coll.a"),
        ("filter", "b"): None,
        ("lookup", "broken"): None,
        ("lookup", "l"): ext.PluginDocs(documentation={"short_description": "L"}, returns={"_raw": {}}),
        ("playbook", "site"): None,
        ("role", "r"): ext.PluginDocs(entry_points={"main": {"short_description": "Main"}, "other": {"options": {}}}),
    }
    assert descriptions(result, "role") == {"r": "Main"}


def test_first_docs_found_win_for_duplicates():
    body = "class FilterModule:\n    def filters(self):\n        return {'shared': f}\n"
    src = MemSource(
        {
            "plugins/filter/a.py": body,
            "plugins/filter/b.py": body,
            "plugins/filter/shared.yml": "DOCUMENTATION:\n  short_description: Shared\n",
        }
    )
    assert plugin_docs(run(src, docs=True), "filter") == {
        "shared": ext.PluginDocs(documentation={"short_description": "Shared"})
    }


# -- doc fragments ----------------------------------------------------------------


def test_doc_fragments_are_collected_raw():
    fragment = '''
class ModuleDocFragment:
    DOCUMENTATION = r"""
options:
  host: {type: str}
"""
    FACTS = "options: {gather: {type: bool}}"
    RETURN = ""
    lowercase = "options: {}"
'''
    src = MemSource(
        {
            "plugins/doc_fragments/auth.py": fragment,
            "plugins/doc_fragments/__init__.py": "",
            "plugins/doc_fragments/net/opts.py": 'class ModuleDocFragment:\n    DOCUMENTATION = "options: {}"\n',
            "plugins/doc_fragments/empty.py": 'class ModuleDocFragment:\n    DOCUMENTATION = "[unclosed"\n',
        }
    )
    result = run(src, docs=True)
    # Fragments aren't plugins.
    assert result.plugins == ()
    assert {name: f.attributes for name, f in result.fragments.items()} == {
        "auth": {
            "DOCUMENTATION": {"options": {"host": {"type": "str"}}},
            "FACTS": {"options": {"gather": {"type": "bool"}}},
        },
        "net.opts": {"DOCUMENTATION": {"options": {}}},
    }
    assert result.issues == ()


@pytest.mark.parametrize(
    ("fragment", "expected"),
    [
        # e.g. dalibo.pglift, whose fragments are generated by a call, and
        # infisical.vault, which uses str.format().
        pytest.param(
            'class ModuleDocFragment:\n    DOCUMENTATION = build()\n    OTHER = "options: {}"\n',
            {"OTHER": {"options": {}}},
            id="call",
        ),
        pytest.param('class Other:\n    DOCUMENTATION = "options: {}"\n', None, id="no-fragment-class"),
        # e.g. amazon.aws's boto3, which sets every attribute in __init__.
        pytest.param(
            "class ModuleDocFragment:\n    def __init__(self):\n        self.DOCUMENTATION = build()\n",
            None,
            id="set-in-method",
        ),
        pytest.param(
            'class ModuleDocFragment:\n    OTHER = "options: {}"\n    async def later(self): ...\n',
            {"OTHER": {"options": {}}},
            id="async-method",
        ),
        pytest.param("class ModuleDocFragment:\n    lower = 'options: {}'\n", None, id="nothing-readable"),
        # e.g. mglantz.demo_collection, an unterminated string.
        pytest.param("print 'python 2'\n", None, id="unparseable"),
    ],
)
def test_unresolved_doc_fragments(fragment, expected):
    result = run(MemSource({"plugins/doc_fragments/frag.py": fragment}), docs=True)
    assert (f.attributes if (f := result.fragments.get("frag")) else None) == expected
    assert issues(result, "fragment_unresolved") == ["plugins/doc_fragments/frag.py"]


# -- YAML that must stay JSON -------------------------------------------------------


def test_yaml_values_are_json_safe():
    doc = """
short_description: Safe
version_added_date: !!timestamp 2024-01-01
blob: !!binary aGVsbG8=
default: .nan
limit: -.inf
choices: !!set {b, a}
ordered: !!omap [{x: 1}, {y: 2}]
keys: {2: int, 1.5: float, true: bool, null: none}
"""
    docs = plugin_docs(run(MemSource({"plugins/modules/m.py": f"DOCUMENTATION = {doc!r}\n"}), docs=True))["m"]
    assert docs.documentation == {
        "short_description": "Safe",
        "version_added_date": "2024-01-01",
        "blob": "aGVsbG8=",
        "default": "nan",
        "limit": "-inf",
        "choices": {"a": None, "b": None},
        "ordered": [["x", 1], ["y", 2]],
        "keys": {"2": "int", "1.5": "float", "true": "bool", "null": "none"},
    }
    _ = json.dumps(dataclasses.asdict(docs), allow_nan=False)


def test_routing_explicit_timestamp_and_numeric_name():
    runtime = "plugin_routing:\n  modules:\n    old:\n      deprecation: {removal_date: !!timestamp 2024-01-01}\n"
    runtime += "    123: {redirect: ns.coll.num}\n"
    assert routing(run(MemSource({"meta/runtime.yml": runtime}))) == {
        "module:123": {"redirect": "ns.coll.num"},
        "module:old": {"deprecation": {"removal_date": "2024-01-01"}},
    }


def _alias_bomb(levels):
    # Each level holds ten references to the one before: 10**levels values
    # when expanded, from a few hundred bytes.
    lines = ["l0: &l0 [x, x, x, x, x, x, x, x, x, x]"]
    lines += [f"l{i}: &l{i} [{', '.join([f'*l{i - 1}'] * 10)}]" for i in range(1, levels + 1)]
    return "\n".join(lines) + "\n"


def test_alias_bombs_are_dropped():
    bomb = _alias_bomb(8)
    src = MemSource(
        {
            "plugins/modules/m.py": f"DOCUMENTATION = 'short_description: Kept'\nRETURN = {bomb!r}\n",
            "meta/runtime.yaml": "plugin_routing:\n  modules:\n    old:\n      redirect: x.y.z\n"
            + "".join(f"      {line}\n" for line in bomb.splitlines()),
        }
    )
    result = run(src, docs=True)
    assert descriptions(result) == {"m": "Kept"}
    assert plugin_docs(result) == {"m": ext.PluginDocs(documentation={"short_description": "Kept"})}
    assert routing(result) == {}
    assert result.issues == (
        ext.Issue(kind="value_too_large", path="plugins/modules/m.py", detail="RETURN"),
        ext.Issue(kind="value_too_large", path="meta/runtime.yaml", detail="module:old"),
    )


def test_deep_nesting_is_dropped():
    deep = "x: " + "[" * 100 + "]" * 100
    src = MemSource(
        {
            "plugins/modules/m.py": f"DOCUMENTATION = {deep!r}\n",
            "roles/r/meta/argument_specs.yml": f"argument_specs:\n  main: {{{deep}}}\n",
            "plugins/doc_fragments/f.py": f"class ModuleDocFragment:\n    DOCUMENTATION = {deep!r}\n",
        }
    )
    result = run(src, docs=True)
    assert [p.docs for p in result.plugins] == [None, None]
    assert result.fragments == {}
    assert [(i.path, i.detail) for i in result.issues] == [
        ("plugins/doc_fragments/f.py", "DOCUMENTATION"),
        ("plugins/modules/m.py", "DOCUMENTATION"),
        ("roles/r/meta/argument_specs.yml", "argument_specs"),
    ]


def test_deep_nesting_with_pure_python_loader(monkeypatch):
    # Without libyaml, PyYAML's composer recurses once per level.
    class PureLoader(yaml.SafeLoader):
        pass

    monkeypatch.setattr(_extract, "_Loader", PureLoader)
    assert _extract._safe_yaml("[" * 100_000 + "]" * 100_000) is None


def test_doc_fragment_routing_is_separate_and_needs_docs():
    runtime = """
plugin_routing:
  modules:
    old: {redirect: ns.coll.new}
  doc_fragments:
    legacy: {redirect: ns.coll.current}
    gone: {tombstone: {removal_version: 2.0.0}}
    bad: not a mapping
"""
    src = MemSource({"meta/runtime.yml": runtime})
    with_docs = run(src, docs=True)
    assert routing(with_docs) == {"module:old": {"redirect": "ns.coll.new"}}
    assert with_docs.fragment_routing == {
        "legacy": {"redirect": "ns.coll.current"},
        "gone": {"tombstone": {"removal_version": "2.0.0"}},
    }
    without = run(src)
    assert routing(without) == routing(with_docs)
    assert without.fragment_routing == {}


def test_fragments_name_their_collection():
    files = {
        "MANIFEST.json": json.dumps({"collection_info": {"namespace": "ns", "name": "coll"}}),
        "plugins/doc_fragments/auth.py": 'class ModuleDocFragment:\n    DOCUMENTATION = "options: {}"\n',
    }
    assert run(MemSource(files), docs=True).fragments["auth"].collection == "ns.coll"
    del files["MANIFEST.json"]
    assert run(MemSource(files), docs=True).fragments["auth"].collection is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Tags that aren't YAML's own are ignored and the value read as written.
        # e.g. prometheus.prometheus's process_exporter role argument_specs.
        pytest.param("!unsafe '{{ x }}'", "{{ x }}", id="ansible-unsafe"),
        pytest.param(
            "!vault |\n    $ANSIBLE_VAULT;1.1;AES256\n    6162", "$ANSIBLE_VAULT;1.1;AES256\n6162\n", id="ansible-vault"
        ),
        pytest.param("!custom {a: [1, !other 2]}", {"a": [1, "2"]}, id="custom-mapping"),
        pytest.param("!custom [a, b]", ["a", "b"], id="custom-sequence"),
        # e.g. famedly.matrix: a Matrix room ID as a RETURN sample. Nothing
        # follows the "tag", so it was meant as text.
        pytest.param("!asdfbuiarbk213e479asf:server.tld", "!asdfbuiarbk213e479asf:server.tld", id="text-like-a-tag"),
        pytest.param("!unsafe ''", "", id="tag-with-empty-value"),
        pytest.param("!!python/object/apply:os.system [echo]", ["echo"], id="python-object"),
        pytest.param("!!python/name:os.system", "", id="python-name"),
        # A standard tag whose value doesn't fit it is read as written too.
        pytest.param("!!int abc", "abc", id="bad-int"),
        pytest.param("!!float x", "x", id="bad-float"),
        pytest.param("!!bool maybe", "maybe", id="bad-bool"),
        pytest.param("!!int {a: 1}", {"a": 1}, id="int-on-a-mapping"),
        # Ones that fit are unchanged.
        pytest.param("!!int 12", 12, id="int"),
        pytest.param("!!null x", None, id="null"),
        pytest.param("!!str 12", "12", id="str"),
    ],
)
def test_yaml_tags(value, expected):
    doc = f"short_description: Kept\nvalue: {value}\n"
    result = run(MemSource({"plugins/modules/m.py": f"DOCUMENTATION = {doc!r}\n"}), docs=True)
    # The rest of the document survives an odd tag.
    assert descriptions(result) == {"m": "Kept"}
    assert plugin_docs(result)["m"].documentation == {"short_description": "Kept", "value": expected}


def test_yaml_tags_in_routing():
    runtime = "plugin_routing:\n  modules:\n    old:\n      deprecation: {removal_version: !!int x, warning_text: !unsafe hi}\n"
    assert routing(run(MemSource({"meta/runtime.yml": runtime}))) == {
        "module:old": {"deprecation": {"removal_version": "x", "warning_text": "hi"}}
    }


# -- Event-Driven Ansible -----------------------------------------------------------


def test_eda_content_in_every_location():
    source_doc = 'DOCUMENTATION = "short_description: {}"\n'
    src = MemSource(
        {
            # Where ansible-rulebook looks, newest layout first.
            "extensions/eda/plugins/event_source/kafka.py": source_doc.format("Kafka"),
            "extensions/eda/plugins/event_sources/webhook.py": source_doc.format("Webhook"),
            "plugins/event_source/legacy.py": source_doc.format("Legacy"),
            "extensions/eda/plugins/event_filter/dashes.py": "",
            "extensions/eda/plugins/event_filters/plural.py": "",
            "plugins/event_filter/old_filter.py": "",
            "extensions/eda/rulebooks/hello.yml": "- name: hello\n",
            "rulebooks/top_level.yml": "",
            # Not loadable, so not listed: a subdirectory (amazon.aws keeps
            # schemas there), a dotted name, __init__, the wrong extension, and
            # a .yaml rulebook (paloaltonetworks.panos has one).
            "extensions/eda/plugins/event_source/schemas/kafka.py": "",
            "extensions/eda/plugins/event_source/my.source.py": "",
            "extensions/eda/plugins/event_source/__init__.py": "",
            "extensions/eda/plugins/event_source/README.md": "",
            "extensions/eda/rulebooks/decryption.yaml": "",
            "extensions/eda/rulebooks/sub/nested.yml": "",
            "extensions/eda/other/thing.py": "",
        }
    )
    result = run(src)
    assert {(p.type, p.name): p.short_description for p in result.plugins} == {
        ("event_filter", "dashes"): None,
        ("event_filter", "old_filter"): None,
        ("event_filter", "plural"): None,
        ("event_source", "kafka"): "Kafka",
        ("event_source", "legacy"): "Legacy",
        ("event_source", "webhook"): "Webhook",
        ("rulebook", "hello"): None,
        ("rulebook", "top_level"): None,
    }


def test_eda_first_location_wins():
    # e.g. chris93111.nats_eda ships the same sources in both layouts.
    # ansible-rulebook loads extensions/eda first, even without docs there.
    src = MemSource(
        {
            "extensions/eda/plugins/event_source/nats.py": "",
            "plugins/event_source/nats.py": 'DOCUMENTATION = "short_description: Old"\n',
            "extensions/eda/rulebooks/site.yml": "",
            "rulebooks/site.yml": "",
        }
    )
    result = run(src, docs=True)
    assert [(p.type, p.name, p.short_description, p.docs) for p in result.plugins] == [
        ("event_source", "nats", None, None),
        ("rulebook", "site", None, None),
    ]


def test_eda_docs_and_unparseable_sources():
    src = MemSource(
        {
            "extensions/eda/plugins/event_source/tick.py": 'DOCUMENTATION = "short_description: Tick"\n'
            + 'EXAMPLES = "- ansible.eda.tick: {}"\n',
            "extensions/eda/plugins/event_filter/broken.py": "print 'python 2'\n",
        }
    )
    result = run(src, docs=True)
    assert {(p.type, p.name): p.docs for p in result.plugins} == {
        ("event_filter", "broken"): None,
        ("event_source", "tick"): ext.PluginDocs(
            documentation={"short_description": "Tick"}, examples="- ansible.eda.tick: {}"
        ),
    }
    assert issues(result, "python_unparseable") == ["extensions/eda/plugins/event_filter/broken.py"]


def test_eda_routing_comes_from_eda_runtime():
    # e.g. ansible.eda, which deprecates sources that moved to other collections.
    eda_runtime = """
plugin_routing:
  event_source:
    aws_sqs_queue:
      deprecation: {removal_version: "3.0.0", warning_text: Please use amazon.aws.aws_sqs_queue instead.}
    moved: {redirect: ns.other.moved}
  event_filter:
    noop:
      tombstone: {removal_version: "3.0.0", warning_text: Stop using it.}
  modules:
    not_eda: {redirect: ns.coll.other}
"""
    # ansible-rulebook doesn't read EDA routing from meta/runtime.yml.
    runtime = "plugin_routing:\n  event_source:\n    ignored: {redirect: ns.coll.x}\n"
    result = run(MemSource({"extensions/eda/eda_runtime.yml": eda_runtime, "meta/runtime.yml": runtime}))
    assert routing(result) == {
        "event_filter:noop": {"tombstone": {"removal_version": "3.0.0", "warning_text": "Stop using it."}},
        "event_source:aws_sqs_queue": {
            "deprecation": {"removal_version": "3.0.0", "warning_text": "Please use amazon.aws.aws_sqs_queue instead."}
        },
        "event_source:moved": {"redirect": "ns.other.moved"},
    }
