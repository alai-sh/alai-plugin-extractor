import dataclasses
import io
import json
import runpy
import sys
import tarfile

import pytest

import alai_plugin_extractor as ext
from alai_plugin_extractor._cli import main

MANIFEST = json.dumps({"collection_info": {"namespace": "ns", "name": "coll", "version": "1.0.0"}})
FILES = {
    "MANIFEST.json": MANIFEST,
    "plugins/modules/thing.py": 'DOCUMENTATION = "short_description: A thing"\n',
    "plugins/lookup/bare.py": "",
    "plugins/filter/dyn.py": "class FilterModule:\n    def filters(self):\n        return build()\n",
    "plugins/filter/old.py": "print 'python 2'\n",
    "meta/runtime.yml": "plugin_routing:\n  modules:\n    old: {redirect: ns.coll.thing}\n",
}


@pytest.fixture
def collection(tmp_path):
    root = tmp_path / "coll"
    for name, content in FILES.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        _ = (root / name).write_text(content)
    return root


def test_json_output_is_the_stored_form(collection, capsys):
    assert main([str(collection)]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == json.loads(json.dumps(dataclasses.asdict(ext.extract(collection))))
    assert err.splitlines() == [
        "warning: filter_test_unresolved: plugins/filter/dyn.py",
        "warning: python_unparseable: plugins/filter/old.py",
        "warning: filter_test_unresolved: plugins/filter/old.py",
    ]


def test_text_output_from_tarball(tmp_path, capsys):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in FILES.items():
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    path = tmp_path / "ns-coll-1.0.0.tar.gz"
    _ = path.write_bytes(buf.getvalue())

    assert main([str(path), "--format", "text"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "ns.coll 1.0.0",
        "lookup  bare",
        "module  thing  A thing",
        'routing module:old {"redirect": "ns.coll.thing"}',
    ]


def test_text_output_without_manifest_or_plugins(tmp_path, capsys):
    assert main([str(tmp_path), "--format", "text"]) == 0
    assert capsys.readouterr() == ("", "")


def test_ansible_core_layout(tmp_path, capsys):
    modules = tmp_path / "lib" / "ansible" / "modules"
    modules.mkdir(parents=True)
    _ = (modules / "ping.py").write_text('DOCUMENTATION = "short_description: Ping"\n')
    assert main([str(tmp_path), "--type", "ansible"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["namespace"], out["name"]) == ("ansible", "builtin")
    assert out["plugins"] == [{"type": "module", "name": "ping", "short_description": "Ping", "docs": None}]


def test_file_that_is_not_a_tarball(tmp_path, capsys):
    path = tmp_path / "notes.txt"
    _ = path.write_text("not a tarball")
    with pytest.raises(SystemExit) as e:
        _ = main([str(path)])
    assert e.value.code == 2
    assert f"can't extract {path}" in capsys.readouterr().err


def test_missing_path(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        _ = main([str(tmp_path / "missing")])
    assert e.value.code == 2
    assert "No such file or directory" in capsys.readouterr().err


def test_python_m_entry_point(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["alai-plugin-extractor", str(tmp_path)])
    with pytest.raises(SystemExit) as e:
        _ = runpy.run_module("alai_plugin_extractor", run_name="__main__")
    assert e.value.code == 0
    assert json.loads(capsys.readouterr().out)["plugins"] == []


def test_docs_output(collection, capsys):
    assert main([str(collection), "--docs"]) == 0
    out = json.loads(capsys.readouterr().out)
    thing = next(p for p in out["plugins"] if p["name"] == "thing")
    assert thing["docs"]["documentation"] == {"short_description": "A thing"}


def test_docs_need_json(collection, capsys):
    with pytest.raises(SystemExit) as e:
        _ = main([str(collection), "--docs", "--format", "text"])
    assert e.value.code == 2
    assert "--docs is only available with --format json" in capsys.readouterr().err
