"""Read-only views of a collection's files. None of them read outside it."""

import abc
import pathlib
import posixpath
import tarfile
import typing as _t

_MAX_MEMBER = 20 * 1024 * 1024


class Source(abc.ABC):
    @abc.abstractmethod
    def files(self) -> set[str]: ...

    @abc.abstractmethod
    def read(self, path: str) -> bytes | None: ...


class DirSource(Source):
    def __init__(self, root: pathlib.Path) -> None:
        self.root = root.resolve()
        self._files: set[str] | None = None

    @_t.override
    def files(self) -> set[str]:
        if self._files is None:
            self._files = {p.relative_to(self.root).as_posix() for p in self.root.rglob("*") if p.is_file()}
        return self._files

    @_t.override
    def read(self, path: str) -> bytes | None:
        p = (self.root / path).resolve()
        if not p.is_relative_to(self.root) or not p.is_file() or p.stat().st_size > _MAX_MEMBER:
            return None
        return p.read_bytes()


class TarSource(Source):
    """An open tarball. Members are read as needed; nothing is written to disk."""

    def __init__(self, tar: tarfile.TarFile) -> None:
        self._tar = tar
        self._members: dict[str, tarfile.TarInfo] = {}
        for m in tar.getmembers():
            name = posixpath.normpath(m.name).lstrip("/").removeprefix("./")
            if name.startswith("..") or not (m.isfile() or m.issym()):
                continue
            self._members[name] = m

    @_t.override
    def files(self) -> set[str]:
        return set(self._members)

    @_t.override
    def read(self, path: str, _depth: int = 0) -> bytes | None:
        m = self._members.get(path)
        if m is None or _depth > 8:
            return None
        if m.issym():
            target = posixpath.normpath(posixpath.join(posixpath.dirname(path), m.linkname))
            return self.read(target, _depth + 1)
        if m.size > _MAX_MEMBER:
            return None
        f = self._tar.extractfile(m)
        return f.read() if f else None


class AnsibleCoreSource(Source):
    """Presents an ansible-core source tree with a collection's layout.

    ansible.builtin keeps modules in lib/ansible/modules/, other plugins in
    lib/ansible/plugins/<type>/, and its routing in
    lib/ansible/config/ansible_builtin_runtime.yml.
    """

    _MAP = (
        ("plugins/modules/", "lib/ansible/modules/"),
        ("plugins/", "lib/ansible/plugins/"),
        ("meta/runtime.yml", "lib/ansible/config/ansible_builtin_runtime.yml"),
    )

    def __init__(self, inner: Source) -> None:
        self.inner = inner

    @_t.override
    def files(self) -> set[str]:
        result: set[str] = set()
        for path in self.inner.files():
            for virtual, real in self._MAP:
                if path.startswith(real):
                    result.add(virtual + path[len(real) :])
                    break
        return result

    @_t.override
    def read(self, path: str) -> bytes | None:
        for virtual, real in self._MAP:
            if path.startswith(virtual):
                return self.inner.read(real + path[len(virtual) :])
        return None
