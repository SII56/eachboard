#!/usr/bin/env python3
"""eachboard: local messaging for cooperating agents; readable output, optional JSON."""
import argparse
import json
import sys
from core import common as c
from commands import (cmd_open, cmd_send, cmd_unread, cmd_read, cmd_convs,
                      cmd_list, cmd_wait, cmd_upload, cmd_download, cmd_ack,
                      cmd_status, cmd_show, cmd_receipts, cmd_init, cmd_doctor)


class Parser(argparse.ArgumentParser):
    def error(self, message):
        c.fail(message, "invalid_arguments")


def nonnegative(value):
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return n


def positive(value):
    n = nonnegative(value)
    if n == 0:
        raise argparse.ArgumentTypeError("must be positive")
    return n


def main():
    p = Parser(prog="python3 eachboard.py", description=__doc__)
    p.add_argument("--json", action="store_true", help="return structured JSON instead of readable text")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("init")
    sp.add_argument("path", nargs="?", default=".")
    sp.set_defaults(fn=cmd_init)
    sp = sub.add_parser("open")
    sp.add_argument("--participants", required=True)
    sp.set_defaults(fn=cmd_open)
    sp = sub.add_parser("send")
    sp.add_argument("text", nargs="?", help="message text (shortcut for --content)")
    source = sp.add_mutually_exclusive_group()
    source.add_argument("--content")
    source.add_argument("--stdin", action="store_true")
    sp.add_argument("--to", help="comma-separated participant names; default broadcast")
    sp.add_argument("--reply-to")
    sp.add_argument("--task-id")
    sp.add_argument("--attachment", action="append", default=[])
    sp.set_defaults(fn=cmd_send)
    for name, fn in [("unread", cmd_unread), ("read", cmd_read), ("peek", cmd_read),
                     ("list", cmd_list), ("wait", cmd_wait)]:
        sp = sub.add_parser(name)
        listed = name == "list"
        sp.add_argument("--limit", type=positive, default=50 if listed else 100)
        sp.add_argument("--after-seq", type=nonnegative, default=None if listed else 0,
                        help="only return messages with a greater sequence; list defaults to the newest page")
        if name in ("read", "wait"):
            sp.add_argument("--ack", action="store_true", help="also mark returned messages read (explicit ack recommended)")
        if name == "wait":
            sp.add_argument("--timeout", type=nonnegative, default=60)
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("ack")
    sp.add_argument("msg_ids", nargs="+")
    sp.set_defaults(fn=cmd_ack)
    for name, fn in [("show", cmd_show), ("receipts", cmd_receipts)]:
        sp = sub.add_parser(name)
        sp.add_argument("msg_id")
        sp.set_defaults(fn=fn)
    sub.add_parser("convs").set_defaults(fn=cmd_convs)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sp = sub.add_parser("upload")
    sp.add_argument("file")
    sp.add_argument("--snapshot", action="store_true", help="preserve a content-addressed copy instead of referencing a project file")
    sp.set_defaults(fn=cmd_upload)
    sp = sub.add_parser("download")
    sp.add_argument("name", help="attachment ID")
    sp.add_argument("dest")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_download)
    for parser in sub.choices.values():
        parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                            help="return structured JSON instead of readable text")
    args = p.parse_args()
    c.JSON_OUTPUT = args.json
    args.fn(args)


if __name__ == "__main__":
    # Preserve structured parse-error output when JSON was explicitly requested.
    c.JSON_OUTPUT = "--json" in sys.argv[1:]
    try:
        main()
    except c.EachboardError as exc:
        c.out({"ok": False, "code": exc.code, "error": str(exc)})
        sys.exit(1)
    except PermissionError as exc:
        c.out({"ok": False, "code": "permission_denied", "error": str(exc)})
        sys.exit(1)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        c.out({"ok": False, "code": "invalid_data", "error": str(exc)})
        sys.exit(1)
    except OSError as exc:
        c.out({"ok": False, "code": "io_error", "error": str(exc)})
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
