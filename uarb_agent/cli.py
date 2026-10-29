"""Command-line entry points for local email processing and mailbox polling."""
import argparse
import json
from pathlib import Path
import sys
import time

from .config import Settings
from .jobs import JobStore, process_lock


def summarize(job):
    """Keep command output useful without dumping email bodies or provider data."""
    fields = (
        "id", "status", "outcome", "file_count", "elapsed_seconds",
        "response_path", "error_type",
    )
    return {field: job[field] for field in fields if field in job}


def build_argument_parser():
    parser = argparse.ArgumentParser(description="Fetch public UARB documents requested by email")
    parser.add_argument("--env", default=".env", help="Configuration file (never committed)")
    commands = parser.add_subparsers(dest="command", required=True)

    process = commands.add_parser("process", help="Process a local RFC822 .eml file")
    process.add_argument("email", type=Path)
    process.add_argument("--send", action="store_true", help="Send via configured SMTP")
    process.add_argument(
        "--retry", action="store_true",
        help="Retry a failed/interrupted job; never resend an uncertain delivery",
    )
    watch = commands.add_parser("watch", help="Poll the configured IMAP mailbox")
    watch.add_argument("--send", action="store_true", help="Send replies instead of only preparing local .eml files")
    watch.add_argument("--once", action="store_true", help="Process one batch, then exit")
    commands.add_parser("doctor", help="Report configuration availability without secrets")
    commands.add_parser("jobs", help="List job statuses")
    send = commands.add_parser("send", help="Send a previously prepared response")
    send.add_argument("job_id")
    return parser


def watch_mailbox(agent, settings, *, send=False, once=False):
    """Acknowledge completed jobs; isolate bad messages so the batch can continue."""
    from .mailbox import Mailbox

    settings.require_imap()
    if send:
        settings.require_smtp()
    completed_statuses = {"prepared", "sent", "ignored", "delivery_unknown", "sending"}

    while True:
        batch_failed = False
        try:
            with Mailbox(
                host=settings.imap_host, port=settings.imap_port,
                user=settings.imap_user, password=settings.imap_password,
                mailbox=settings.imap_mailbox, secure=settings.imap_secure,
                max_email_bytes=settings.max_email_bytes,
                recipient=settings.imap_recipient,
            ) as mailbox:
                for uid, raw in mailbox.fetch_unseen(limit=20):
                    try:
                        job = agent.process(raw, send=send)
                    except Exception as exc:
                        # One malformed email must not prevent other senders' jobs.
                        batch_failed = True
                        print(json.dumps({"event": "message_error", "uid": uid,
                                          "type": type(exc).__name__}), file=sys.stderr, flush=True)
                        continue
                    print(json.dumps(summarize(job)), flush=True)
                    if job["status"] in {"failed", "delivery_unknown", "sending"} or job.get("outcome") == "error":
                        batch_failed = True
                    if job["status"] in completed_statuses:
                        mailbox.mark_seen(uid)
                for uid, reason in mailbox.skipped:
                    print(json.dumps({"event": "message_skipped", "uid": uid, "reason": reason}),
                          file=sys.stderr, flush=True)
        except Exception as exc:
            batch_failed = True
            print(json.dumps({"event": "mailbox_error", "type": type(exc).__name__}),
                  file=sys.stderr, flush=True)
        if once:
            return 1 if batch_failed else 0
        time.sleep(settings.poll_seconds)


def main(argv=None):
    args = build_argument_parser().parse_args(argv)
    try:
        settings = Settings.from_env(args.env)
        if args.command == "doctor":
            print(json.dumps({
                "python": sys.version.split()[0],
                "jev_configured": bool(settings.typesafe_api_key),
                "model": settings.typesafe_model,
                "imap_configured": bool(settings.imap_host and settings.imap_user and settings.imap_password),
                "smtp_configured": bool(settings.smtp_host and settings.smtp_user and settings.smtp_password),
                "data_dir": str(settings.data_dir.resolve()),
                "download_workers": settings.download_workers,
            }, indent=2))
            return 0
        if args.command == "jobs":
            # Atomic manifests can be read while a watcher holds the write lock.
            print(json.dumps([summarize(job) for job in JobStore(settings.data_dir).list()], indent=2))
            return 0

        from .service import Agent

        with process_lock(settings.data_dir):
            agent = Agent(settings)
            if args.command == "process":
                if args.email.stat().st_size > settings.max_email_bytes:
                    raise ValueError("Input email exceeds MAX_EMAIL_BYTES")
                job = agent.process(args.email.read_bytes(), send=args.send, retry=args.retry)
                print(json.dumps(summarize(job), indent=2))
                return int(job["status"] in {"failed", "delivery_unknown", "sending"} or job.get("outcome") == "error")
            if args.command == "send":
                job = agent.jobs.read(args.job_id)
                if not job:
                    raise ValueError("Job not found")
                result = agent.deliver(job)
                print(json.dumps(summarize(result), indent=2))
                return 0 if result["status"] == "sent" else 1
            return watch_mailbox(agent, settings, send=args.send, once=args.once)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
