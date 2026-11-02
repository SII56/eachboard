"""Explicit execution context for one identity and project."""
import json
import os
import hmac
import sys
from pathlib import Path
from .storage import MessageStore
from .display import render, use_color


def discover(start=None):
    start = Path(start or Path.cwd()).resolve()
    for parent in (start, *start.parents):
        if (parent / '.eachboard').exists() or (parent / '.eachboard').is_symlink():
            return parent / '.eachboard'
    return None


class BoardContext:
    def __init__(self, environ=None, cwd=None, json_output=False):
        self.environ = dict(os.environ if environ is None else environ)
        self.cwd = Path.cwd().resolve() if cwd is None else Path(cwd).resolve()
        configured = (self.environ.get('EACHBOARD_ROOT') or '').strip()
        found = self.discover()
        self.root = (self.cwd / Path(configured).expanduser()).resolve() if configured else found
        self.root_source = ('EACHBOARD_ROOT environment' if configured else
                            'project discovery' if found is not None else 'not found')
        self.conv = self.environ.get('EACHBOARD_CONV')
        self.conv_source = None
        self.who = None
        self.key = self.environ.get('EACHBOARD_KEY')
        self.json_output = json_output
        self.store = MessageStore(self.root, self.who)
        self.workspace = self.store.workspace

    def discover(self):
        return discover(self.cwd)

    @property
    def is_admin(self):
        return self.who == 'admin'

    def require_participant(self):
        if self.is_admin:
            self.store.fail('administrator can open and inspect conversations, but cannot send, ack, retract or wait;'
                            ' use list/show/read for inspection', 'admin_read_only')

    def resolve_identity(self, cid=None, admin_only=False, metadata=None):
        """Resolve a key inside this project; never trust a caller-supplied name."""
        self.who = self.store.who = None
        self.store.is_admin = False
        if self.root is None:
            self.store.fail('no .eachboard project found from ' + str(self.cwd), 'project_not_found')
        self.store.verify_format()
        if not self.key:
            self.store.fail('set EACHBOARD_KEY in the invoking environment', 'missing_credentials')
        digest = self.store.sha(self.key)
        admin = self.root / 'admin.hash'
        if not admin.is_file():
            self.store.fail('administrator key is missing; initialize a new project', 'invalid_data')
        if hmac.compare_digest(digest, admin.read_text(encoding='ascii')):
            self.who = self.store.who = 'admin'
            self.store.is_admin = True
            return self.who
        if admin_only:
            self.store.fail('administrator key required', 'unauthorized')
        keys = (metadata if metadata is not None else self.store.read_metadata(cid))['keys']
        matches = [name for name, hashed in keys.items() if hmac.compare_digest(digest, hashed)]
        if len(matches) > 1:
            self.store.fail('ambiguous participant key in conversation metadata', 'invalid_data')
        if not matches:
            self.store.fail('key rejected for the selected conversation; check EACHBOARD_KEY and EACHBOARD_CONV',
                            'unauthorized')
        self.who = self.store.who = matches[0]
        return self.who

    def authenticate_admin(self):
        self.resolve_identity(admin_only=True)

    def out(self, obj):
        text = (json.dumps(obj, ensure_ascii=False) if self.json_output else
                render(obj, color=use_color(sys.stdout, self.environ)))
        print(text, flush=True)

    def current_conv(self):
        self.who = self.store.who = None
        self.store.is_admin = False
        if self.root is None:
            self.store.fail("no .eachboard project found from " + str(self.cwd)
                 + "; run python3 -m eachboard init in the shared project, set EACHBOARD_ROOT, or run python3 -m eachboard doctor",
                 "project_not_found")
        self.store.verify_format()
        explicit = (self.environ.get('EACHBOARD_CONV') or '').strip()
        self.conv = explicit or self.store.read_head()
        self.conv_source = 'EACHBOARD_CONV environment' if explicit else 'HEAD'
        if not self.key:
            self.store.fail("EACHBOARD_KEY must be set in the invoking environment;"
                 " run python3 -m eachboard doctor for the selected root and conversation", "missing_credentials")
        if not self.conv:
            self.store.fail("no active conversation in " + str(self.root)
                 + "; set EACHBOARD_CONV or run python3 -m eachboard open; run python3 -m eachboard doctor for context",
                 "missing_credentials")
        source = self.conv_source
        path = self.store.conv_dir(self.conv) / "meta"
        if not path.is_file():
            self.store.fail("conversation " + str(self.conv) + " not found in " + str(self.root)
                 + " (selected by " + source + "); check EACHBOARD_CONV or run python3 -m eachboard doctor",
                 "conversation_not_found")
        meta = self.store.read_metadata(self.conv)
        self.resolve_identity(self.conv, metadata=meta)
        return self.conv
