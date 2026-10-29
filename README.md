# UARB email document agent

A Python agent that reads an email, identifies one matter and document category with Jev, retrieves up to ten public files from the UARB database, and prepares a ZIP and a factual email reply. It can run against local `.eml` files or poll an IMAP mailbox and send through SMTP.

The website integration uses the same session-bound HTTP requests that FileMaker WebDirect uses. It includes initialization, search, category selection, export, and file download. **Chrome and a browser agent are not runtime dependencies.**

## Run the demo

Requires Python 3.11+ on macOS or Linux, internet access, and a TypeSafe API key. No email-provider account is required for the local demo.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
cp .env.example .env
# Edit .env: set TYPESAFE_API_KEY. Never commit this file.
uarb-agent doctor
uarb-agent process examples/request.eml
```

The sample asks for Other Documents from M12205, with the matter number in the body rather than the subject. The command prints a job ID and the path to the prepared reply. Under `data/jobs/<job-id>/` it saves:

| File | What it proves |
| --- | --- |
| `interpretation.json` | Jev's validated decision and confidence audit |
| `request.json` | The typed retrieval request |
| `retrieval.json` | Website metadata, five counts, files, sizes, hashes, and warnings |
| `downloads/` | Downloaded public files |
| `documents.zip` | Verified ZIP attachment, when files exist and fit the limit |
| `response.eml` | Complete reply, including the attachment |
| `job.json` | Durable status and timings |

Open `response.eml` in a mail client to inspect the reply. The local command does not send it. Run `uarb-agent process examples/ambiguous.eml` for a clarification and `uarb-agent process examples/empty.eml` for a category with zero files. Jev is a remote classifier, so synthetic examples are not guarantees of model behavior.

Repeated identical inputs reuse the persisted result. To repeat a timed live demo, use a new data directory: `DATA_DIR=data/demo-fresh uarb-agent process examples/request.eml`. `--retry` retries interrupted/failed jobs or prepared error replies, but never automatically resends an uncertain SMTP delivery.

## Components and flow

Each module has one main responsibility:

| Module | Responsibility |
| --- | --- |
| `cli.py` | Commands, configuration status, and mailbox polling loop |
| `config.py` | Environment settings and required provider configuration |
| `models.py` | Shared typed records and request validation |
| `interpret.py` | MIME parsing and Jev classification |
| `uarb.py` / `protocol.py` | Website session, request replay, and streamed downloads |
| `delivery.py` | ZIP verification, reply formatting, MIME, and SMTP |
| `mailbox.py` | IMAP connection, unread fetching, and acknowledgement |
| `service.py` | End-to-end orchestration and explicit retries |
| `jobs.py` | Atomic persistence, deduplication, and process locking |

```text
RFC822 email / IMAP
        |
        v
MIME parsing + loop checks (interpret.py)
        |
        v
One Jev request: intent + matter + category
        |
        v
Validation + confidence gates
        |                    |
        | retrieve           | ambiguous/missing -> clarification template
        v
Cookie session + WebDirect HTTP replay (uarb.py, protocol.py)
        |
        v
Public-only downloads -> size/hash checks -> verified ZIP (delivery.py)
        |
        v
Deterministic email template -> saved .eml -> optional SMTP

service.py + jobs.py track the operation and prevent duplicate work/sends.
```

1. **Intake:** `mailbox.py` uses IMAP `BODY.PEEK` so reading does not acknowledge early. `interpret.py` reads the subject and body, including conversational context; it does not assume the subject contains the matter. Automated messages and self-replies are ignored.
2. **Interpretation:** candidate matter numbers are extracted mechanically; Jev chooses only from allowed values and the five canonical categories. The intermediate object is constructed and validated in Python. Jev does not generate JSON or the outgoing prose. Missing or uncertain requests produce a clarification instead of a download.
3. **Retrieval:** `uarb.py` opens an HTTP cookie session, obtains the server's security token and connector IDs, replays search, reads metadata and category counts, and selects the first up to ten explicitly Public rows. It handles the filename prompt used by transcripts and recordings. `protocol.py` contains the observed request shapes, with runtime identifiers substituted.
4. **File lifecycle:** each export is downloaded while its dialog remains alive, then the dialog closes. Streams have a total byte budget, use temporary files, validate PDF signatures, and calculate SHA-256. There are no live browser interactions or model calls in this loop.
5. **Packaging and reply:** `delivery.py` verifies downloaded hashes, streams a ZIP, rechecks archived hashes/CRC, and renders a template containing actual metadata, counts, and downloaded count. It checks the full MIME message size, including base64 overhead.
6. **Persistence and delivery:** atomic JSON manifests and a process lock provide a simple single-process deployment. SMTP status is persisted before sending. A lost connection after submission becomes `delivery_unknown`; the operator checks the mailbox before any manual recovery. This avoids claiming impossible exactly-once email delivery.

## Why this implementation

The exploration compared browser DOM actions, browser-initialized request replay, and entirely HTTP-based initialization/replay. It also tested three independent sessions downloading in parallel.

| Prototype, same ten files, two runs each | Mean |
| --- | ---: |
| Browser DOM, one worker | 8.663 s |
| Browser initialization + request replay, one worker | 9.567 s |
| HTTP initialization + request replay, one worker | 8.492 s |
| HTTP initialization + request replay, three workers | 10.042 s |

The roughly 2% observed difference between DOM and HTTP is within the variation of this small test. The stronger benefit is removing Chrome, selectors, screenshot/model calls, and browser deployment overhead. Three workers sped up the download phase but their extra session setup made total time worse. This release therefore supports **one worker**.

Sharing one UI session between parallel actions caused state races. Closing an export before fetching its URL produced 404s. Save List/print gave inventories, not a replacement for export resources. An authenticated official Data API was not available for this anonymous portal, and URL-based matter selection was not established. These findings justify the current protocol adapter, rather than assuming every site has a clean records API.

See [prototype benchmark evidence](docs/benchmarks.json), [four synthetic ambiguity examples](docs/jev-ambiguity-checks.json), and the [Loom walkthrough](docs/LOOM_DEMO.md). The benchmark report's `remainingWork` describes the earlier prototype, not this implementation's status. Four examples are a smoke check, not a statistical accuracy evaluation.

## Live email demo and mailbox setup

**The email flow is working and verified.** A request sent from an external university account was received through Gmail IMAP, processed into ten public documents, and returned through Gmail SMTP as a ZIP. The recipient confirmed that the reply and attachment arrived. See [the verification record](docs/live-mail-verification.json).

### For reviewers

Send a normal email to the demo address provided with the submission:

> Please send me the Other Documents for matter M12205.

The agent replies to the sender with matter metadata, the five category counts, and a ZIP of up to ten public documents. Reviewers do not install software, connect a mailbox, or provide credentials. The personal demo address is shared separately rather than published in this repository. The demo worker must be running when a request is sent.

### To run your own instance

After installing the project and configuring `TYPESAFE_API_KEY`, run:

```sh
python -m uarb_agent.mail_setup
uarb-agent watch --send
```

On macOS, double-click `Start Live Demo.command` to perform those steps together. The setup prompts privately for a Gmail address and Google app password, verifies both IMAP and SMTP authentication, and stores credentials in the ignored local `.env`. Existing TypeSafe configuration is preserved. Subsequent launches verify the saved mailbox settings.

The setup uses `imap.gmail.com:993` and `smtp.gmail.com:465` with TLS and polls every ten seconds. It configures a `+senpilot` receiving alias and sets `IMAP_RECIPIENT` so only unread messages addressed to that alias are fetched. Ordinary messages addressed to the base Gmail address are excluded. The reply uses the connected Gmail account as its sender.

```sh
python -m uarb_agent.mail_setup --check-only  # Verify login without reading/sending mail
uarb-agent doctor                          # Report configuration without secrets
uarb-agent jobs                            # Inspect processing and delivery status
```

Generic IMAP/SMTP settings are also supported through `.env.example`. `watch --once` prepares one batch of replies locally; `send <job-id>` sends a prepared reply. `ALLOWED_SENDERS` can restrict requesters when needed. See [the operator guide](docs/LIVE_DEMO.md) for startup and hosting instructions.

## Verify and submit

```sh
python -m pytest -q
```

The review expanded the offline suite to **75 passing tests**. It covers MIME parsing, constrained model responses, protocol behavior, failed/incomplete downloads, ZIP integrity, size limits, interrupted jobs, SMTP uncertainty, duplicate requests, and mailbox batch isolation.

Five synthetic emails were also run through **real Jev + the public UARB website + ZIP/MIME preparation**:

| Example | Verified result |
| --- | --- |
| Matter number only in the body | M12205 Other Documents, 10 PDFs |
| Body corrects an outdated subject | M12205 Key Documents, 6 PDFs |
| Written hearing record, explicitly not audio | M12451 Transcripts, 7 PDFs |
| Category with no files | M12205 Transcripts, no attachment |
| Two unresolved matter numbers | Clarification, no retrieval or attachment |

All five passed after a prompt correction: the first test run unnecessarily clarified the straightforward body-only request. The final prompt explicitly explains that this is a dedicated UARB request inbox, so the sender need not spell out “UARB.” Confidence thresholds remain unchanged. Both the [initial results](docs/validation-initial-results.json) and [final results](docs/validation-results.json) are retained, with a [validation summary](docs/validation.md). These are useful integration examples, not an estimate of general model accuracy.

To rerun those five checks explicitly, spending a small amount of TypeSafe credit and downloading public documents:

```sh
python tests/live_pipeline.py
```

The live runner checks the actual MIME attachment against the saved ZIP, CRC, every file's size and SHA-256, counts, metadata, and duplicate-input behavior. It writes fresh local jobs and replaces `docs/validation-results.json`; it never sends email. The expected file counts reflect the tested public database and may need updating if its contents change.

Tests use mocked Jev/HTTP/IMAP/SMTP responses; live checks are separate and explicit. Dependencies are declared in `pyproject.toml`, and `requirements.lock` pins the tested environment used by setup and CI. CI runs the offline tests on Python 3.12. A Dockerfile is included for a configured IMAP/SMTP deployment. Its image has not been built in this environment because the Docker daemon was unavailable; external inbox delivery was confirmed in the live acceptance check.

The repository excludes `.env`, virtual environments, job data, downloaded documents, and the confidential assignment PDF. Configure credentials privately and provide the Loom recording and demo receiving address with the submission.
