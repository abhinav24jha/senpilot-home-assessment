"""Explicit live smoke suite: five real Jev + UARB runs, never SMTP.

Run from the repository root with: python tests/live_pipeline.py
This is intentionally outside pytest discovery because it spends API credit and
uses the public portal. Inputs are synthetic and all outputs stay under data/.
"""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
import hashlib
import io
import json
from pathlib import Path
import time
import zipfile

from uarb_agent.config import Settings
from uarb_agent.models import CATEGORIES
from uarb_agent.service import Agent

CASES = [
    {"name": "body_only_matter", "subject": "A quick regulatory research request",
     "body": "Hi Agent,\n\nPlease send me the Other Documents files for matter M12205.\n\nThanks, Alex",
     "matter": "M12205", "category": "Other Documents", "files": 10, "outcome": "success"},
    {"name": "corrected_subject", "subject": "M12383 Exhibits request",
     "body": "Correction to the subject: please ignore M12383 and Exhibits. What I need now is Key Documents for M12205. Please send those files only.",
     "matter": "M12205", "category": "Key Documents", "files": 6, "outcome": "success"},
    {"name": "paraphrased_transcripts", "subject": "Written hearing record",
     "body": "Could you send me the written transcripts of what was said at the hearings in M12451? I need the written record, not audio recordings.",
     "matter": "M12451", "category": "Transcripts", "files": 7, "outcome": "success"},
    {"name": "zero_files", "subject": "Transcript request",
     "body": "Please send Transcripts for matter M12205.",
     "matter": "M12205", "category": "Transcripts", "files": 0, "outcome": "success"},
    {"name": "ambiguous_matter", "subject": "Exhibits request",
     "body": "Please send the Exhibits, but I cannot remember whether the matter is M12205 or M12383. I do not know which one is correct. Please do not send both.",
     "matter": None, "category": None, "files": 0, "outcome": "clarification"},
]


def validate_reply(job, directory, expected):
    """Inspect the actual MIME attachment, not merely the source ZIP on disk."""
    response = Path(job["response_path"]).read_bytes()
    assert hashlib.sha256(response).hexdigest() == job["response_sha256"]
    message = BytesParser(policy=policy.default).parsebytes(response)
    assert message["To"] == "alex@example.test"
    assert message["Auto-Submitted"] == "auto-replied"
    attachments = list(message.iter_attachments())
    body = message.get_body(preferencelist=("plain",)).get_content()
    checks = ["prepared response hash", "synthetic recipient", "auto-reply header"]
    interpretation = json.loads((directory / "interpretation.json").read_text())
    if expected["outcome"] == "clarification":
        assert interpretation["action"] == "clarify" and interpretation["request"] is None
        assert not attachments and not (directory / "retrieval.json").exists()
        assert "clarification" in body.lower()
        return {"checks": checks + ["clarification without retrieval or attachment"],
                "jev_audit": interpretation["audit"], "mime_bytes": len(response)}
    request = json.loads((directory / "request.json").read_text())
    result = json.loads((directory / "retrieval.json").read_text())
    assert request["matter_number"] == result["matter_number"] == expected["matter"]
    assert request["document_type"] == result["document_type"] == expected["category"]
    assert set(result["counts"]) == set(CATEGORIES)
    assert result["metadata"]["title"] and result["metadata"]["status"]
    assert len(result["files"]) == expected["files"]
    for category, count in result["counts"].items():
        assert f"{category}: {count}" in body
    assert expected["matter"] in body
    checks += ["correct interpreted matter/category", "all five website counts in reply", "matter metadata", "expected file count"]
    total = sum(file["size"] for file in result["files"])
    if expected["files"]:
        assert len(attachments) == 1
        assert attachments[0].get_content_type() == "application/zip"
        payload = attachments[0].get_payload(decode=True)
        assert payload == (directory / "documents.zip").read_bytes()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            assert len(archive.infolist()) == expected["files"]
            assert archive.testzip() is None
            for file in result["files"]:
                raw = archive.read(file["filename"])
                assert len(raw) == file["size"]
                assert hashlib.sha256(raw).hexdigest() == file["sha256"]
                assert Path(file["path"]).read_bytes() == raw
                assert raw.startswith(b"%PDF-")
        checks += ["MIME ZIP equals saved ZIP", "ZIP entry count and CRC", "every source/archive size and SHA-256", "PDF signatures"]
    else:
        assert not attachments and "No downloadable files" in body
        checks.append("zero result has no attachment")
    return {"checks": checks, "jev_audit": interpretation["audit"], "counts": result["counts"],
            "file_count": len(result["files"]), "download_bytes": total, "mime_bytes": len(response)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=[case['name'] for case in CASES])
    parser.add_argument('--report', type=Path, default=Path('docs/validation-results.json'))
    args = parser.parse_args(argv)
    cases = [case for case in CASES if not args.case or case['name'] == args.case]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    settings = replace(Settings.from_env(), data_dir=Path("data") / ("validation-" + stamp))
    agent = Agent(settings)
    results = []
    for case in cases:
        message = EmailMessage()
        message["From"] = "alex@example.test"
        message["To"] = settings.agent_from
        message["Subject"] = case["subject"]
        message["Message-ID"] = f"<{stamp}.{case['name']}@example.test>"
        message.set_content(case["body"])
        started = time.monotonic()
        report = {"name": case["name"], "input": {"subject": case["subject"], "body": case["body"]},
                  "expected": {key: case[key] for key in ("matter", "category", "files", "outcome")}}
        try:
            job = agent.process(message.as_bytes())
            report.update(job_id=job["id"], status=job["status"], outcome=job.get("outcome"),
                          pipeline_seconds=round(time.monotonic()-started, 3))
            directory = agent.jobs.path(job["id"])
            interpretation_path = directory / "interpretation.json"
            if interpretation_path.exists():
                report["jev_audit"] = json.loads(interpretation_path.read_text())["audit"]
            assert job["status"] == "prepared" and job["outcome"] == case["outcome"], report
            (directory / "input.eml").write_bytes(message.as_bytes())
            report.update(validate_reply(job, directory, case))
            before = (directory / "job.json").read_bytes()
            duplicate = agent.process(message.as_bytes())
            assert duplicate["id"] == job["id"] and (directory / "job.json").read_bytes() == before
            report["checks"].append("repeat input reuses unchanged job")
            report["passed"] = True
        except Exception as exc:
            report.update(passed=False, failure_type=type(exc).__name__)
        results.append(report)
        print(json.dumps({key: report.get(key) for key in ("name", "passed", "status", "outcome", "file_count", "pipeline_seconds", "failure_type")}), flush=True)
    summary = {"run_utc": stamp, "scope": "Synthetic emails, real Jev and public UARB; local MIME replies only, no SMTP or IMAP.",
               "data_directory": str(settings.data_dir), "passed": sum(result["passed"] for result in results),
               "total": len(results), "cases": results}
    destination = args.report
    destination.parent.mkdir(exist_ok=True)
    destination.write_text(json.dumps(summary, indent=2) + "\n")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
