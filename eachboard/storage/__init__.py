"""Project-bound storage facade; operations share one root and identity.

The implementation is grouped into cooperating storage components. Components
call through self so cross-component operations use the same project state.
No module reads credentials or discovers projects at import time.
"""
from . import files
from .project import ProjectStorage
from .objects import ObjectStorage
from .index import IndexStorage
from .journals import JournalStorage
from .queries import MessageQueries
from .metadata import MetadataStorage
from ..errors import fail


class MessageStore(ProjectStorage, MetadataStorage, ObjectStorage, IndexStorage, JournalStorage, MessageQueries):
    def __init__(self, root, who):
        self.root = root
        self.who = who
        self.is_admin = False
        self.workspace = root.parent if root is not None and root.name == '.eachboard' else None

    fail = staticmethod(fail)
    component = staticmethod(files.component)
    identity = staticmethod(files.identity)
    sha = staticmethod(files.sha)
    sender_digest = staticmethod(files.sender_digest)
    atomic_write = staticmethod(files.atomic_write)
    lock = staticmethod(files.lock)
    publishing = staticmethod(files.publishing)

    def conv_dir(self, cid):
        return self.root / "conversations" / self.component(cid, "conversation id")
