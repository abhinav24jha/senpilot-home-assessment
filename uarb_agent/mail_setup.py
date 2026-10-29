"""Configure the demo Gmail inbox and verify login without reading or sending mail."""
from dataclasses import replace
from getpass import getpass
from pathlib import Path
import argparse
import smtplib
import ssl

from dotenv import dotenv_values, set_key

from .config import Settings
from .mailbox import Mailbox, MailboxError


def check_mail_connections(settings: Settings):
    """Verify IMAP folder access and SMTP authentication; do not send a message."""
    settings.require_imap()
    settings.require_smtp()
    with Mailbox(settings.imap_host, settings.imap_port, settings.imap_user,
                 settings.imap_password, settings.imap_mailbox, settings.imap_secure):
        pass
    context = ssl.create_default_context()
    if settings.smtp_secure:
        connection = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30, context=context)
    else:
        connection = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30)
    with connection as smtp:
        smtp.ehlo()
        if not settings.smtp_secure:
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(settings.smtp_user, settings.smtp_password)


def configure_gmail(env_path: Path, address: str, app_password: str):
    """Verify supplied credentials before replacing any existing mailbox settings."""
    address = address.strip().lower()
    password = ''.join(app_password.split())
    if address.count('@') != 1 or not address.endswith(('@gmail.com', '@googlemail.com')):
        raise ValueError('Use the personal Gmail address dedicated to this demo.')
    if not password:
        raise ValueError('Enter a Google app password, not your usual account password.')
    local_name, domain = address.split('@')
    recipient = local_name.split('+')[0] + '+senpilot@' + domain
    settings = replace(Settings.from_env(env_path), agent_from=address,
        imap_host='imap.gmail.com', imap_port=993, imap_secure=True,
        imap_user=address, imap_password=password, imap_mailbox='INBOX',
        imap_recipient=recipient,
        smtp_host='smtp.gmail.com', smtp_port=465, smtp_secure=True,
        smtp_user=address, smtp_password=password, poll_seconds=10)
    check_mail_connections(settings)
    values = {
        'AGENT_FROM': address, 'IMAP_HOST': settings.imap_host, 'IMAP_PORT': '993',
        'IMAP_SECURE': 'true', 'IMAP_USER': address, 'IMAP_PASSWORD': password,
        'IMAP_MAILBOX': 'INBOX', 'SMTP_HOST': settings.smtp_host, 'SMTP_PORT': '465',
        'IMAP_RECIPIENT': recipient,
        'SMTP_SECURE': 'true', 'SMTP_USER': address, 'SMTP_PASSWORD': password,
        'POLL_SECONDS': '10',
    }
    env_path.touch(mode=0o600, exist_ok=True)
    env_path.chmod(0o600)
    for name, value in values.items():
        set_key(str(env_path), name, value)
    return address


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--env', type=Path, default=Path('.env'))
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args(argv)
    try:
        settings = Settings.from_env(args.env)
        if args.check_only or (settings.imap_user and settings.smtp_user):
            check_mail_connections(settings)
            address = settings.agent_from
        else:
            print('One-time setup for the agent inbox. Reviewers do not need to do this.')
            print('Use a dedicated Gmail account with 2-Step Verification enabled.')
            print('Create an app password named UARB agent at https://myaccount.google.com/apppasswords')
            print('Use the generated 16-character app password, not your normal Gmail password.')
            address = input('Demo Gmail address: ')
            password = getpass('Google app password (hidden): ')
            address = configure_gmail(args.env, address, password)
        recipient = settings.imap_recipient or dotenv_values(args.env).get('IMAP_RECIPIENT') or address
        print(f'Mailbox login verified. Demo receiving address: {recipient}')
        print('No messages were read or sent during this connection check.')
        return 0
    except (EOFError, KeyboardInterrupt):
        print('\nSetup cancelled; the watcher has not started.')
        return 1
    except MailboxError as exc:
        print(f'Mailbox setup failed: {exc}')
        return 1
    except smtplib.SMTPAuthenticationError:
        print('IMAP worked, but SMTP rejected the login. Check the Google app password and account restrictions.')
        return 1
    except Exception as exc:
        # Provider errors may echo credentials; report the class, not server text.
        print(f'Mailbox setup failed ({type(exc).__name__}). Check the address, app password, and connection.')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
