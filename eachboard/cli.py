"""eachboard: local messaging for cooperating agents; readable output, optional JSON."""
import argparse
import json
import sys
from .context import BoardContext
from .errors import EachboardError, fail
from .commands import (
    cmd_init, cmd_open, cmd_status, cmd_doctor, cmd_convs,
    cmd_send, cmd_read, cmd_list, cmd_ack, cmd_show, cmd_retract,
    cmd_receipts, cmd_wait,
)


class Parser(argparse.ArgumentParser):

    def error(self, message):
        fail(message, 'invalid_arguments')


def nonnegative(value):
    n = int(value)
    if n < 0:
        raise argparse.ArgumentTypeError('must be nonnegative')
    return n


def positive(value):
    n = nonnegative(value)
    if n == 0:
        raise argparse.ArgumentTypeError('must be positive')
    return n


def build_parser():
    p = Parser(prog="python3 -m eachboard", description=__doc__,
               epilog="Init sets or generates an administrator key. Commands require EACHBOARD_KEY."
                      " Inject the administrator key as EACHBOARD_KEY to open or inspect"
                      " all conversations. Identity is resolved from the key."
                      " EACHBOARD_CONV selects a conversation; otherwise HEAD is used."
                      " Administrator viewing never marks participant messages seen or acknowledged.")
    p.add_argument("--json", action="store_true", help="return structured JSON instead of readable text")
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("init")
    sp.add_argument("path", nargs="?", default=".")
    sp.set_defaults(fn=cmd_init)
    sp = sub.add_parser("open", description="Create a conversation using the administrator EACHBOARD_KEY established by init.")
    sp.add_argument("--participants", required=True)
    sp.set_defaults(fn=cmd_open)
    sp = sub.add_parser("send", description="Send text; use exactly one of TEXT, --content, --stdin or --file.")
    sp.add_argument("text", nargs="?", help="message text (shortcut for --content)")
    source = sp.add_mutually_exclusive_group()
    source.add_argument("--content")
    source.add_argument("--stdin", action="store_true")
    source.add_argument("--file", metavar="PATH", help="read the message body from a UTF-8 draft file")
    sp.add_argument("--delete-on-success", action="store_true",
                    help="delete the --file draft after the message is sent, unless it changed meanwhile")
    sp.add_argument("--to", help="comma-separated participant names; default broadcast")
    sp.add_argument("--reply-to")
    sp.add_argument("--task-id")
    sp.set_defaults(fn=cmd_send)
    for name, fn, summary in [
        ("read", cmd_read, "display new messages; use --pending to revisit unfinished work; admin reads history without state changes"),
        ("list", cmd_list, "display message history, including your own messages"),
        ("wait", cmd_wait, "wait for new messages, then display them"),
    ]:
        sp = sub.add_parser(name, help=summary, description=summary)
        listed = name == "list"
        sp.add_argument("--limit", type=positive, default=50 if listed else 100)
        sp.add_argument("--after-seq", type=nonnegative, default=None if listed else 0,
                        help="only return messages with a greater sequence; list defaults to the newest page")
        sp.add_argument("--task-id", help="only messages with this task ID, plus notifications"
                        " retracting one; other messages stay unseen")
        if name in ("read", "wait"):
            sp.add_argument("--ack", action="store_true", help="also acknowledge the returned messages (explicit ack recommended)")
        if name == "read":
            sp.add_argument("--pending", action="store_true",
                            help="show messages not yet acknowledged instead of new ones")
        if name == "wait":
            sp.add_argument("--timeout", type=nonnegative, default=60)
        sp.set_defaults(fn=fn)
    sp = sub.add_parser("ack", help="acknowledge messages already displayed to you")
    sp.add_argument("msg_ids", nargs="+")
    sp.set_defaults(fn=cmd_ack)
    sp = sub.add_parser("show")
    sp.add_argument("msg_id")
    sp.add_argument("--force", action="store_true",
                    help="show the original body of a retracted message")
    sp.set_defaults(fn=cmd_show)
    sp = sub.add_parser("receipts")
    sp.add_argument("msg_id")
    sp.set_defaults(fn=cmd_receipts)
    sp = sub.add_parser("retract")
    sp.add_argument("msg_id")
    sp.set_defaults(fn=cmd_retract)
    sub.add_parser("convs", help="list all project conversations as admin, or your authenticated conversation as a participant").set_defaults(fn=cmd_convs)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    for parser in sub.choices.values():
        parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                            help="return structured JSON instead of readable text")
    return p



def main(argv=None, context=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ctx = BoardContext() if context is None else context
    ctx.json_output = '--json' in argv
    try:
        args = build_parser().parse_args(argv)
        ctx.json_output = args.json
        args.fn(ctx, args)
        return 0
    except EachboardError as exc:
        ctx.out({'ok': False, 'code': exc.code, 'error': str(exc)})
    except PermissionError as exc:
        ctx.out({'ok': False, 'code': 'permission_denied', 'error': str(exc)})
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        ctx.out({'ok': False, 'code': 'invalid_data', 'error': str(exc)})
    except OSError as exc:
        ctx.out({'ok': False, 'code': 'io_error', 'error': str(exc)})
    except KeyboardInterrupt:
        return 130
    return 1
