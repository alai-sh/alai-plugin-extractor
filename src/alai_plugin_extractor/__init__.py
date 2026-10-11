"""Static extraction of plugin metadata from Ansible collections.

Collections are untrusted input: nothing is imported or executed. See
`extract()` for what is read and how.

This is an internal component of alai, not a public library. It's available
to use, but its interface and behaviour can change in any release without
warning or deprecation.
"""

from ._extract import extract, extract_bytes
from ._fragments import DocFragment, FragmentLookup
from ._types import Collection, Issue, IssueKind, Plugin, PluginDocs, Routing, SourceType, fragment_lookup

__all__ = [
    "Collection",
    "DocFragment",
    "FragmentLookup",
    "Issue",
    "IssueKind",
    "Plugin",
    "PluginDocs",
    "Routing",
    "SourceType",
    "extract",
    "extract_bytes",
    "fragment_lookup",
]
