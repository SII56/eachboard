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
        self.who = self.environ.get('EACHBOARD_WHO')
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

    def authenticate_admin(self):
        """Authenticate an injected administrator identity for inspection."""
        self.store.is_admin = False
        if self.root is None:
            self.store.fail('no .eachboard project found from ' + str(self.cwd), 'project_not_found')
        self.store.verify_format()
        if not self.who or not self.key:
            self.store.fail('set EACHBOARD_WHO=admin and EACHBOARD_KEY to the administrator password',
                            'missing_credentials')
        if not self.is_admin:
            self.store.fail('administrator identity required', 'unauthorized')
        self.verify_admin_password(self.key)
        self.store.is_admin = True

    def verify_admin_password(self, password, create=False, confirm=None):
        """Verify the password independently of the calling participant identity."""
        if not password:
            self.store.fail('administrator password must not be empty')
        path = self.root / 'admin.hash'
        with self.store.lock(self.root / '.admin.lock'):
            if not path.exists():
                if not create:
                    self.store.fail('administrator password has not been established; run open first',
                                    'unauthorized')
                if confirm is not None and confirm() != password:
                    self.store.fail('administrator password confirmation does not match')
                self.store.atomic_write(path, self.store.sha(password))
            if not hmac.compare_digest(self.store.sha(password), path.read_text(encoding='ascii')):
                self.store.fail('administrator password rejected', 'unauthorized')

    def out(self, obj):
        text = (json.dumps(obj, ensure_ascii=False) if self.json_output else
                render(obj, color=use_color(sys.stdout, self.environ)))
        print(text, flush=True)

    def current_conv(self):
        self.store.is_admin = False
        if self.root is None:
            self.store.fail("no .eachboard project found from " + str(self.cwd)
                 + "; run python3 -m eachboard init in the shared project, set EACHBOARD_ROOT, or run python3 -m eachboard doctor",
                 "project_not_found")
        self.store.verify_format()
        explicit = (self.environ.get('EACHBOARD_CONV') or '').strip()
        self.conv = explicit or self.store.read_head()
        self.conv_source = 'EACHBOARD_CONV environment' if explicit else 'HEAD'
        if not self.key or not self.who:
            self.store.fail("EACHBOARD_WHO and EACHBOARD_KEY must both be set in the invoking environment;"
                 " run python3 -m eachboard doctor for the selected root and conversation", "missing_credentials")
        if not self.conv:
            self.store.fail("no active conversation in " + str(self.root)
                 + "; set EACHBOARD_CONV or run python3 -m eachboard open; run python3 -m eachboard doctor for context",
                 "missing_credentials")
        self.store.identity(self.who)
        source = self.conv_source
        if self.is_admin:
            self.authenticate_admin()
        path = self.store.conv_dir(self.conv) / "meta"
        if not path.is_file():
            self.store.fail("conversation " + str(self.conv) + " not found in " + str(self.root)
                 + " (selected by " + source + "); check EACHBOARD_CONV or run python3 -m eachboard doctor",
                 "conversation_not_found")
        meta = self.store.read_metadata(self.conv)
        if not self.is_admin and not hmac.compare_digest(self.store.sha(self.key), meta["keys"].get(self.who, "")):
            self.store.fail("key rejected for " + str(self.who) + " in conversation " + str(self.conv)
                 + " (" + str(self.root) + ", selected by " + source + "); check EACHBOARD_KEY or run python3 -m eachboard doctor",
                 "unauthorized")
        return self.conv
