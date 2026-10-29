# Live email demo

Reviewers only need the agent's receiving email address. They send a normal email from their own account, for example:

> Please send me the Other Documents for matter M12205.

A successful reply contains the matter details, five category counts, and a ZIP of ten public documents. No installation, credentials, or account connection is required from the reviewer.

## One-time operator setup

The Gmail setup creates a `+senpilot` receiving alias and configures `IMAP_RECIPIENT` to search only unread messages addressed to that alias before fetching their bodies. This keeps ordinary personal mail out of the agent. A dedicated Gmail account is also suitable. Share only the receiving alias with reviewers; it still reveals the underlying Gmail account name.

1. Sign into that Gmail account and enable 2-Step Verification if needed.
2. Create a Google app password named `UARB agent` at https://myaccount.google.com/apppasswords.
3. On this Mac, double-click `Start Live Demo.command`. Enter the Gmail address and app password into its prompts. The password is hidden, checked against both IMAP and SMTP, and saved only to the ignored local `.env`. The existing TypeSafe key is preserved.
4. The same window starts the watcher after authentication succeeds. Copy the receiving address printed there into the submission instructions.
5. Send the example from a separate address, such as the university account. Keep the received ZIP and screenshot for the Loom. Check the sender's spam folder if SMTP accepted the message but it is not in the inbox.

Google login/2-Step Verification must be completed by the account owner. Connecting Gmail to a chat assistant alone does not grant credentials to this independently running Python program.

For a terminal on macOS or Linux, use:

```sh
python -m uarb_agent.mail_setup
uarb-agent watch --send
```

The setup command verifies existing configured credentials on subsequent launches. To check without starting the watcher, run `python -m uarb_agent.mail_setup --check-only`. The probe does not read or send emails. A successful probe proves authentication; the university-email round trip proves delivery and attachment handling.

## Availability during review

The Mac launcher prevents idle sleep while its terminal stays open, but closing the laptop, losing connectivity, or terminating the process stops responses. This is suitable for a scheduled live demo. It is not an always-on hosted deployment.

For reviewers to test whenever they want, deploy this same repository to an always-on Linux machine, create an `uarb` service account, put the repository and its virtual environment at `/opt/uarb-agent`, and supply the private `.env` there. Give that service account ownership of the application/data directory and restrict `.env` to mode 600. Install `deploy/uarb-agent.service` into `/etc/systemd/system/`, then run:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now uarb-agent
sudo journalctl -u uarb-agent -f
```

The service file is a deployment template. No cloud host has been provisioned or verified yet. Do not describe the demo as available 24/7 until the deployed worker and a complete email round trip are verified.

## Status and sources

A real external request has been received through Gmail IMAP and the ten-document ZIP reply accepted by Gmail SMTP. The actual MIME attachment was checked for file counts, sizes, SHA-256 values, and CRC. The recipient confirmed receipt of the reply and ZIP in their external inbox; see `live-mail-verification.json`.

Google documents [app passwords and their 2-Step Verification requirement](https://support.google.com/accounts/answer/185833) and [Gmail IMAP/SMTP endpoints](https://developers.google.com/workspace/gmail/imap/imap-smtp). Some organization or protected accounts do not offer app passwords; this setup targets a personal Gmail account dedicated to the demo.
