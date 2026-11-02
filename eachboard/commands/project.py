"""Project command handlers; each receives an explicit context."""
import hmac
import getpass
import secrets
import sys
import time
from pathlib import Path
from ..errors import EachboardError
from ..storage.index import INDEX_EMPTY
from ..storage.project import FORMAT_MARKER


def cmd_init(ctx, args):
    workspace = (ctx.cwd / Path(args.path).expanduser()).resolve()
    if not workspace.is_dir():
        ctx.store.fail("project directory not found")
    root = workspace / ".eachboard"
    if root.exists() or root.is_symlink():
        ctx.store.fail("eachboard already initialized", "already_exists")
    generated = False
    if sys.stdin.isatty():
        key = getpass.getpass('Admin key (type your own, or press Enter to generate): ')
        if key:
            if getpass.getpass('Confirm admin key: ') != key:
                ctx.store.fail('administrator key confirmation does not match')
        else:
            key = secrets.token_urlsafe(32)
            generated = True
    else:
        key = ctx.environ.get('EACHBOARD_KEY')
        if not key:
            key = secrets.token_urlsafe(32)
            generated = True
    with ctx.store.publishing(workspace, ".eachboard") as d:
        ctx.store.atomic_write(d / "FORMAT", FORMAT_MARKER)
        ctx.store.atomic_write(d / "HEAD", "")
        ctx.store.atomic_write(d / "admin.hash", ctx.store.sha(key))
        (d / ".admin.lock").touch(mode=0o600)
        (d / "conversations").mkdir(mode=0o700)
    ignore = workspace / ".gitignore"
    text = ignore.read_text() if ignore.exists() else ""
    if ".eachboard/" not in text.splitlines():
        ctx.store.atomic_write(ignore, text + ("\n" if text and not text.endswith("\n") else "") + ".eachboard/\n", mode=0o644)
    result = {"ok": True, "root": str(root), "workspace": str(workspace)}
    if generated:
        result.update(admin_key=key, note='Save this admin key now; it will not be shown again.')
    ctx.out(result)


def cmd_open(ctx, args):
    if ctx.root is None:
        ctx.store.fail("no .eachboard project found from " + str(ctx.cwd)
               + "; run python3 -m eachboard init in the project directory", "project_not_found")
    ctx.store.verify_format()
    participants = [ctx.store.identity(p.strip(), "participant") for p in args.participants.split(",")]
    if len(set(participants)) != len(participants):
        ctx.store.fail("duplicate participants")
    if 'admin' in participants:
        ctx.store.fail("admin is reserved for the project administrator")
    ctx.authenticate_admin()
    cid = secrets.token_hex(8)
    keys = {p: secrets.token_urlsafe(24) for p in participants}
    with ctx.store.publishing(ctx.root / "conversations", cid) as d:
        (d / "messages").mkdir()
        (d / "state").mkdir()
        (d / "index").write_bytes(INDEX_EMPTY)
        (d / ".send.lock").touch(mode=0o600)
        ctx.store.write_metadata(d, {"participants": participants,
                     "keys": {p: ctx.store.sha(k) for p, k in keys.items()}, "created": time.time()})
    ctx.store.write_head(cid)
    ctx.out({"ok": True, "conv_id": cid, "keys": keys})


def cmd_status(ctx, args):
    cid = ctx.current_conv()
    totals = {'unseen': 0, 'pending': 0} if ctx.is_admin else ctx.store.counts(cid)
    ctx.out({"ok": True, "conv_id": cid, "who": ctx.who, "root": str(ctx.root),
           "participants": ctx.store.read_metadata(cid)["participants"],
           "conversation_source": ctx.conv_source,
           "ack_required": not ctx.is_admin, "unseen": totals["unseen"], "pending": totals["pending"],
           "role": "administrator" if ctx.is_admin else "participant",
           "workspace": str(ctx.workspace) if ctx.workspace else None})


def _env_flag(ctx, name):
    value = ctx.environ.get(name)
    return "set" if value and value.strip() else "unset"


def cmd_doctor(ctx, args):
    # Diagnostics must work without credentials, without a project, and never print secrets.
    env_root = (ctx.environ.get("EACHBOARD_ROOT") or "").strip()
    env_conv = (ctx.environ.get("EACHBOARD_CONV") or "").strip()
    discovered = ctx.discover()
    warnings = []
    root_source = ctx.root_source
    if env_root:
        configured = (ctx.cwd / Path(env_root).expanduser()).resolve()
        if discovered is not None and configured != discovered:
            warnings.append("EACHBOARD_ROOT overrides the project eachboard at " + str(discovered)
                            + "; unset EACHBOARD_ROOT to use the project")
    if ctx.root is None:
        warnings.append("no .eachboard project found from " + str(ctx.cwd)
                        + "; run python3 -m eachboard init in the shared project or set EACHBOARD_ROOT")

    head_conv = None
    storage_supported = None
    if ctx.root is not None:
        try:
            ctx.store.verify_format()
            storage_supported = True
        except (EachboardError, OSError) as exc:
            storage_supported = False
            warnings.append(str(exc))
        if storage_supported:
            try:
                head_conv = ctx.store.read_head()
            except (EachboardError, OSError) as exc:
                warnings.append(str(exc))

    cid = env_conv or head_conv
    conv_source = "EACHBOARD_CONV environment" if env_conv else ("HEAD" if head_conv else None)
    if env_conv and head_conv and env_conv != head_conv:
        warnings.append("EACHBOARD_CONV overrides the project conversation " + str(head_conv))

    conv_exists = False
    participants = None
    meta_path = None
    if cid and ctx.root is not None and storage_supported:
        try:
            ctx.store.component(str(cid), "conversation id")
            meta_path = ctx.root / "conversations" / str(cid) / "meta"
        except EachboardError:
            warnings.append("invalid conversation id in environment or HEAD: " + str(cid))
        if meta_path is not None and meta_path.is_file():
            conv_exists = True
            try:
                participants = ctx.store.read_metadata(cid).get("participants")
            except (EachboardError, OSError, ValueError) as exc:
                warnings.append("cannot read conversation metadata: " + str(exc))
        elif meta_path is not None:
            warnings.append("selected conversation not found: " + str(cid))
    elif cid:
        warnings.append("cannot inspect conversation " + str(cid) + " without a supported project root")
    else:
        warnings.append("no active conversation; run python3 -m eachboard open or set EACHBOARD_CONV")

    have_key = bool(ctx.key)
    authorized = None
    ctx.who = ctx.store.who = None
    ctx.store.is_admin = False
    if not have_key:
        warnings.append('EACHBOARD_KEY is not set; authentication skipped')
    elif storage_supported:
        try:
            # Admin resolution is independent of HEAD; participant resolution
            # requires the selected conversation to exist.
            if conv_exists:
                ctx.resolve_identity(cid)
            else:
                ctx.authenticate_admin()
            authorized = True
        except (EachboardError, OSError, ValueError) as exc:
            authorized = False
            warnings.append('cannot verify credentials: ' + str(exc))

    ctx.out({
        "ok": True,
        "root": str(ctx.root) if ctx.root is not None else None,
        "root_source": root_source,
        "workspace": str(ctx.workspace) if ctx.workspace is not None else None,
        "project": str(discovered) if discovered is not None else None,
        "storage_supported": storage_supported,
        "environment": {
            "EACHBOARD_ROOT": _env_flag(ctx, "EACHBOARD_ROOT"),
            "EACHBOARD_CONV": _env_flag(ctx, "EACHBOARD_CONV"),
            "EACHBOARD_KEY": _env_flag(ctx, "EACHBOARD_KEY"),
        },
        "conversation": {"id": cid, "source": conv_source, "exists": conv_exists,
                         "head": head_conv},
        "identity": {"who": ctx.who, "key_present": have_key, "authorized": authorized},
        "participants": participants,
        "warnings": warnings,
    })


def cmd_convs(ctx, args):
    try:
        ctx.authenticate_admin()
    except EachboardError as exc:
        if exc.code != 'unauthorized':
            raise
    if not ctx.is_admin:
        cid = ctx.current_conv()
        conversations = [{"conv_id": cid}]
    else:
        ctx.authenticate_admin()
        conversations = []
        for directory in sorted((ctx.root / 'conversations').iterdir()):
            if directory.name.startswith('.tmp-') or not directory.is_dir():
                continue
            meta = ctx.store.read_metadata(directory.name)
            conversations.append({'conv_id': directory.name,
                                  'participants': meta['participants'], 'created': meta['created']})
    ctx.out({"ok": True, "conversations": conversations})
