"""Small project files: FORMAT identifies storage; HEAD selects a conversation."""
from ..errors import EachboardError

FORMAT_MARKER = b'eachboard 2\n'


class ProjectStorage:
    def verify_format(self):
        if not self.root.is_dir():
            self.fail('eachboard is not initialized at ' + str(self.root), 'not_initialized')
        path = self.root / 'FORMAT'
        try:
            with path.open('rb') as stream:
                marker = stream.read(len(FORMAT_MARKER) + 1)
        except FileNotFoundError:
            marker = None
        if marker != FORMAT_MARKER:
            self.fail('unsupported project storage format at ' + str(self.root)
                      + '; use a new initialized project or the matching older eachboard version',
                      'unsupported_format')

    def read_head(self):
        path = self.root / 'HEAD'
        try:
            with path.open('rb') as stream:
                raw = stream.read(130)
        except FileNotFoundError:
            return None
        if raw in (b'', b'\n'):
            return None
        if raw.endswith(b'\n'):
            raw = raw[:-1]
        try:
            value = raw.decode('ascii')
            self.component(value, 'conversation id')
        except (UnicodeDecodeError, EachboardError):
            self.fail('invalid HEAD at ' + str(path) + '; expected one conversation ID',
                      'invalid_head')
        return value

    def write_head(self, cid):
        cid = self.component(cid, 'conversation id')
        self.atomic_write(self.root / 'HEAD', cid + '\n')
