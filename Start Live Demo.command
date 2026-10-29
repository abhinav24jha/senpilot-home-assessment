#!/bin/bash
cd "$(dirname "$0")" || exit 1
if ! test -x .venv/bin/python; then
  echo 'Install the project using the README first.'
  read -r -p 'Press Enter to close.'
  exit 1
fi
if ! .venv/bin/python -m uarb_agent.mail_setup; then
  read -r -p 'Press Enter to close.'
  exit 1
fi
echo 'Agent is running. Send a request from your university email to the address above.'
echo 'Keep this window open and the laptop lid open. Press Control-C to stop.'
exec /usr/bin/caffeinate -i .venv/bin/uarb-agent watch --send
