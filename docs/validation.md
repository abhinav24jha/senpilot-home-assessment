# Pipeline validation

The final test suite exercises parsing, interpretation, retrieval, packaging, persistence, and transport boundaries. Offline tests use controlled provider/transport responses; separate live checks call the real Jev API and public UARB site. All email addresses in the examples are synthetic. The live runner **prepares local MIME replies and never sends email**.

## Five live examples

The five-case rerun passed **5/5** after the intent-prompt correction described below. Each input used a fresh job, not a previously cached retrieval. Exact emails, classification audits, file counts, bytes, checks, and timings are in [validation-results.json](validation-results.json).

| Example | Expected and observed result | Pipeline time |
| --- | --- | ---: |
| Ordinary subject; M12205 and Other Documents appear only in the body | 10 PDFs, 7,009,940 bytes, ZIP attached | 15.567 s |
| Subject requests M12383 Exhibits; body explicitly corrects it to M12205 Key Documents | 6 PDFs, 1,070,395 bytes, ZIP attached | 7.773 s |
| Body asks for the written hearing record, explicitly excluding audio, for M12451 | 7 transcript PDFs, 5,178,346 bytes, ZIP attached | 10.729 s |
| Request for M12205 Transcripts | Correct zero result, all five counts included, no ZIP | 3.505 s |
| Sender cannot choose between M12205 and M12383 | Clarification, no retrieval and no ZIP | 0.190 s |

For successful retrievals, validation reads the actual outgoing MIME message, extracts its ZIP, checks its entry count and CRC, and matches every archived file's size and SHA-256 against the saved download manifest and source bytes. It checks PDF signatures, interpreted matter/category, matter metadata, all five counts in the reply, and the saved email hash. Every example is replayed once locally to verify deduplication returns the same unchanged job.

The timings measure interpretation through prepared email, including downloads and ZIP creation. They exclude validation checks and SMTP/IMAP. These are single smoke runs, not performance benchmarks; server/network variability explains why they differ from earlier timings.

## A failure found and fixed

The first five-case run passed 4/5. A clear request saying “Please send me the Other Documents files for matter M12205” correctly selected the matter and category, but Jev assigned REQUEST intent confidence 0.72 and selected probability 0.86. The unchanged thresholds of 0.8 and 0.9 therefore caused an unnecessary clarification.

The intent prompt now states that this is a dedicated UARB request inbox and that ordinary requests to send/get the materials count even when the email does not repeat the acronym “UARB.” Negation, historical-context rules, and thresholds were preserved. The same five inputs then passed; this is evidence for the tested examples, not a general accuracy guarantee. The original failure and audit remain in [validation-initial-results.json](validation-initial-results.json).

After the last service/protocol refactor, the ten-document case passed once more against the final code in **11.335 seconds**, again producing ten verified PDFs and the correct MIME ZIP. See [validation-final-smoke.json](validation-final-smoke.json) for that independent result.

## Offline coverage

The offline suite contains **69 passing tests** covering:

- MIME/plain/HTML parsing, attachment exclusion, body-only matter IDs, quoted history, corrections, ambiguous choices, and confidence gates.
- Malformed provider results, unknown choices, invalid scores, authentication/rate-limit/server errors, timeouts, and sanitized errors.
- Self-reply, automated-message, bulk/list, and bounce exclusion; allowed-sender filtering.
- Exact request identifiers, supported categories, integer document limits, and safe filenames.
- Public-only selection, all-five count extraction, overlapping categories, protocol-state cache invalidation, restricted download origins, interrupted streams, content-length mismatch, invalid PDF data, and byte limits.
- ZIP hash/CRC verification, modified-source detection, duplicate filenames, cleanup, MIME attachment round trips, metadata rendering, and ZIP/full-message size limits.
- Success, clarification, zero-result and error replies; preserved timestamps and removal of stale artifacts when retrying a failed request.
- Duplicate inputs, explicit retry of interrupted retrieval, prepared-message delivery, changed-message rejection, uncertain SMTP acceptance, and no automatic resend after a send-stage interruption.
- TLS/authentication and explicit recipient envelopes, IMAP size checks and BODY.PEEK, acknowledgement, UIDVALIDITY, one bad message not blocking the next, and reading job status while the worker lock is held.

## Reproduce

```sh
python -m pytest -q
```

The live checks require the configured TypeSafe key and internet access. They consume API credit and download public files, so pytest does not run them automatically.

```sh
python tests/live_pipeline.py
# Target one case without replacing the five-case report:
python tests/live_pipeline.py --case body_only_matter --report docs/validation-final-smoke.json
```

Real mailbox credentials were not configured. IMAP/SMTP adapters were tested with controlled mocks, including failure paths, but delivery to a real inbox remains a deployment acceptance check. These five live examples do not exercise a large recording attachment; earlier category exploration proved the export route and this implementation's separate smoke check rejected an oversized recording at the header-size guard.
