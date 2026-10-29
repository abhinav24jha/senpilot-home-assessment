"""Browser-free public UARB WebDirect adapter.

The transport is a stateful UI protocol, not a published records API. Only one
control request may be in flight per application state. Export resources remain
valid only while their export state is alive.
"""
from __future__ import annotations

import copy
import hashlib
import html
import json
import re
import time
import uuid
from pathlib import Path
from typing import Callable
from urllib.parse import unquote_plus, urlencode, urlsplit

import httpx

from .models import CATEGORIES, DocumentRequest, DownloadedFile, RetrievalError, RetrievalResult, SizeLimitError
from .protocol import BUTTON_CLICK_TEMPLATE, LAYOUT_ACKNOWLEDGEMENTS, MATTER_PLACEHOLDER, SEARCH_TEMPLATE

BASE = "https://uarb.novascotia.ca/fmi/webd/"
PORTAL = BASE + "UARB15"
ORIGIN = "https://uarb.novascotia.ca"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36"
EXPORT_PATH = re.compile(r'app://(APP/connector/[^"\\]+/dl/[^"\\]+)')
ROW_ID = re.compile(r"^(b\d+p\d+)o\d+(i(\d+)i\d+r\d+)$")
CATEGORY_COUNT = re.compile(r"^(" + "|".join(map(re.escape, CATEGORIES)) + r") - (\d+)$")


def plain_text(value: object) -> str:
    return html.unescape(re.sub(r"<[^>]*>", " ", str(value or ""))).strip()


def safe_filename(value: str) -> str:
    # FileMaker resource paths encode spaces as +. Never allow remote path names
    # to escape the job directory, including Windows-style separators.
    value = unquote_plus(value).replace("\\", "/").rsplit("/", 1)[-1]
    value = re.sub(r'[\x00-\x1f\x7f:*?"<>|]', "_", value).strip(" .")
    if not value or value in {".", ".."}:
        raise RetrievalError("Website returned an invalid export filename")
    if len(value) > 180:
        suffix = Path(value).suffix[:15]
        value = value[:160] + suffix
    return value


class UarbClient:
    def __init__(self, client: httpx.Client | None = None):
        self.http = client or httpx.Client(
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            timeout=httpx.Timeout(30, connect=15),
        )
        self.owns_client = client is None
        self.state: dict[str, dict] = {}
        self.initial_state: dict[str, dict] = {}
        self._entries_cache: list[dict] | None = None
        self._connector_ids: dict[str, str] | None = None
        self.sequence = {"syncId": 0, "clientId": 0}
        self.ui_id: int | None = None
        self.uidl_url = ""
        self.export_paths: list[str] = []
        self.last_heartbeat = time.monotonic()

    def close(self):
        if self.owns_client:
            self.http.close()

    def _ingest(self, messages: list[dict]):
        for message in messages:
            if message.get("meta", {}).get("appError"):
                raise RetrievalError("UARB session expired or its application protocol changed")
            for name in ("syncId", "clientId"):
                value = message.get(name)
                if isinstance(value, int) and value >= 0:
                    self.sequence[name] = max(self.sequence[name], value)
            if message.get("meta", {}).get("repaintAll"):
                self.state = {}
                self._entries_cache = None
            if message.get("state"):
                self._entries_cache = None
            self.state.update(message.get("state") or {})
            self.export_paths.extend(EXPORT_PATH.findall(json.dumps(message)))

    def rpc(self, commands: list, **extra):
        response = self.http.post(self.uidl_url, json={**self.sequence, "rpc": commands, **extra})
        response.raise_for_status()
        try:
            messages = json.loads(response.text.removeprefix("for(;;);"))
            if not isinstance(messages, list):
                raise ValueError("Expected message list")
            self._ingest(messages)
        except (ValueError, TypeError, AttributeError) as exc:
            raise RetrievalError("Unexpected UARB protocol response") from exc

    def resync(self):
        self.rpc([], resynchronize=True)

    def until(self, predicate: Callable[[], bool], description: str, timeout: float = 20):
        started = time.monotonic()
        attempt = 0
        while not predicate():
            if time.monotonic() - started > timeout:
                raise RetrievalError(f"UARB did not return {description}; the matter may be unavailable or the site changed")
            if attempt:
                time.sleep(min(.5, .05 * attempt))
            self.resync()
            attempt += 1

    def entries(self) -> list[dict]:
        # Several operations inspect the same response. Normalize its labels once.
        if self._entries_cache is None:
            self._entries_cache = [
                {**value, "key": key, "label": plain_text(value.get("caption") or value.get("text"))}
                for key, value in self.state.items()
            ]
        return self._entries_cache

    def find(self, label: str) -> dict | None:
        return next((entry for entry in self.entries() if entry["label"] == label), None)

    def _find_component_key(self, predicate: Callable[[dict], bool], state: dict | None = None) -> str:
        for key, value in (state if state is not None else self.state).items():
            if predicate(value):
                return key
        raise RetrievalError("Required UARB interface component is missing")

    def id_map(self) -> dict[str, str]:
        # Search controls are resolved from the immutable initial layout, once.
        if self._connector_ids is not None:
            return self._connector_ids
        initial = self.initial_state
        layout = self._find_component_key(lambda value: value.get("id") == "c0layoutcontainer", initial)
        self._connector_ids = {
            "33": layout,
            "5": self._find_component_key(lambda value: layout in value.get("childData", {}), initial),
            "3": self._find_component_key(lambda value: bool(value.get("layoutLinks")), initial),
            "97": self._find_component_key(lambda value: value.get("inputPrompt") == "eg M01234", initial),
            "106": self._find_component_key(lambda value: value.get("id") == "b0p0o258i0i0r1", initial),
        }
        return self._connector_ids

    def map_batch(self, batch: list, matter: str | None = None) -> list:
        mapped = copy.deepcopy(batch)
        identifiers = self.id_map()
        for command in mapped:
            command[0] = identifiers.get(command[0], command[0])
            if command[2] == "layoutClick":
                command[3][1] = identifiers.get(command[3][1], command[3][1])
            if matter is not None:
                command[3] = [matter + "\n" if value == MATTER_PLACEHOLDER else value
                              for value in command[3]]
        return mapped

    def click(self, identifier: str):
        commands = copy.deepcopy(BUTTON_CLICK_TEMPLATE)
        for command in commands:
            if command[2] == "layoutClick":
                command[0] = self.id_map()[command[0]]
                command[3][1] = identifier
            else:
                command[0] = identifier
        self.rpc(commands)

    def acknowledge_layout(self):
        for batch in LAYOUT_ACKNOWLEDGEMENTS:
            self.rpc(self.map_batch(batch))

    def initialize(self):
        response = self.http.get(PORTAL)
        response.raise_for_status()
        app = re.search(r'vaadin.initApplication\("([^"]+)"', response.text)
        if not app:
            raise RetrievalError("UARB initialization page changed")
        params = {
            "v-browserDetails": "1", "theme": "default", "v-appId": app[1],
            "v-sh": "1200", "v-sw": "1280", "v-cw": "1280", "v-ch": "1200",
            "v-curdate": str(int(time.time() * 1000)), "v-tzo": "240", "v-dstd": "60",
            "v-rtzo": "300", "v-dston": "true", "v-tzid": "America/Toronto",
            "v-vw": "1280", "v-vh": "1200", "v-loc": str(response.url),
            "v-wn": app[1] + "-" + uuid.uuid4().hex,
        }
        separator = "&" if "?" in str(response.url) else "?"
        response = self.http.post(str(response.url) + separator + "v-" + str(int(time.time() * 1000)),
                                  content=urlencode(params), headers={"Content-Type": "application/x-www-form-urlencoded"})
        response.raise_for_status()
        try:
            root = response.json()
            initial = json.loads(root["uidl"])
            self.ui_id = root["v-uiId"]
            self.uidl_url = BASE + "UIDL/?v-uiId=" + str(self.ui_id)
            self.sequence["csrfToken"] = initial["Vaadin-Security-Key"]
            self._ingest([initial])
        except (KeyError, ValueError, TypeError) as exc:
            raise RetrievalError("UARB returned an incomplete session") from exc
        ui = self._find_component_key(lambda value: bool(value.get("pageState")))
        app_rpc = self._find_component_key(lambda value: bool(value.get("rpcInterfaces")))
        self.rpc([
            [ui, "com.vaadin.shared.ui.DelayedCallbackRpc", "received", [0]],
            [app_rpc, "com.filemaker.jwpc.iwp.widgetset.client.rpc.AppServerRpc", "setClientOrigin", [ORIGIN]],
            [app_rpc, "com.filemaker.jwpc.iwp.widgetset.client.rpc.AppServerRpc", "setRetinaDisplay", [False]],
        ], wsver="8.27.7.fmi")
        self.rpc([[ui, "com.vaadin.shared.ui.ui.UIServerRpc", "resize", [1280, 1200, 1280, 1200]]])
        self.until(lambda: any(e.get("inputPrompt") == "eg M01234" for e in self.entries()), "the search form")
        self.initial_state = copy.deepcopy(self.state)
        self.acknowledge_layout()
        return self

    def counts(self) -> dict[str, int]:
        counts = {}
        for entry in self.entries():
            match = CATEGORY_COUNT.fullmatch(entry["label"])
            if match:
                counts[match[1]] = int(match[2])
        return counts

    def search(self, matter: str) -> tuple[dict[str, str], dict[str, int]]:
        for batch in SEARCH_TEMPLATE:
            self.rpc(self.map_batch(batch, matter=matter))
        self.resync()
        self.until(lambda: bool(self.find(matter)) and len(self.counts()) == 5, "the requested matter and all category counts")
        metadata = self.metadata()
        if self._field(286) != matter:
            raise RetrievalError("Returned matter does not match the requested matter")
        return metadata, self.counts()

    def _field(self, object_id: int) -> str:
        return next((entry["label"] for entry in self.entries() if entry.get("id") == f"b0p0o{object_id}i0i0r1"), "")

    def metadata(self) -> dict[str, str]:
        # These are FileMaker field-object identifiers, not per-session connector
        # identifiers. Require the known layout instead of silently mislabeling data.
        data = {"title": self._field(290), "status": self._field(289), "type": self._field(287),
                "category": self._field(298), "outcome": self._field(295),
                "date_received": self._field(292), "date_value": self._field(294),
                "date_label": self._field(310), "source_url": PORTAL}
        if not data["title"] or not data["date_label"]:
            raise RetrievalError("UARB matter metadata layout changed")
        if data["date_label"] == "Decision Date":
            data["decision_date"] = data["date_value"]
        return data

    def category(self, category: str):
        tab = next((entry for entry in self.entries() if entry["label"].startswith(category + " - ")), None)
        if tab is None:
            raise RetrievalError("Requested category is missing")
        self.click(tab["key"])
        self.acknowledge_layout()
        self.resync()
        self.until(lambda: any(e["label"] == "GO GET IT" for e in self.entries()), "document rows")

    def rows(self) -> list[dict]:
        entries = self.entries()
        # Group the fields by row once, instead of scanning every field for each
        # download button. Preserve label order for inventory-change detection.
        row_labels: dict[tuple[str, str], list[str]] = {}
        for entry in entries:
            match = ROW_ID.match(entry.get("id", ""))
            if match and entry["label"]:
                row_labels.setdefault((match[1], match[2]), []).append(entry["label"])
        rows = []
        for entry in entries:
            if entry["label"] != "GO GET IT":
                continue
            match = ROW_ID.match(entry.get("id", ""))
            if not match:
                continue
            labels = row_labels.get((match[1], match[2]), [])
            rows.append({"row": int(match[3]), "key": entry["key"],
                         "public": "Public" in labels and "Confidential" not in labels, "labels": labels})
        return sorted(rows, key=lambda row: row["row"])

    def open_download(self, row: dict) -> tuple[str, str]:
        before = len(self.export_paths)
        self.click(row["key"])
        self.resync()
        self.until(lambda: len(self.export_paths) > before or bool(self.find("OK")), "an export resource or filename prompt")
        confirmation = self.find("OK")
        if confirmation:
            mouse = {"altKey": False, "button": "LEFT", "clientX": 0, "clientY": 0,
                     "ctrlKey": False, "metaKey": False, "relativeX": 10, "relativeY": 10,
                     "shiftKey": False, "type": 1}
            self.rpc([[confirmation["key"], "com.vaadin.shared.ui.button.ButtonServerRpc", "click", [mouse]]])
            self.until(lambda: len(self.export_paths) > before, "the confirmed export resource")
        path = self.export_paths[before]
        url = BASE + path
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "uarb.novascotia.ca" or not parsed.path.startswith("/fmi/webd/APP/connector/"):
            raise RetrievalError("Unexpected download origin")
        return url, safe_filename(path.rsplit("/", 1)[-1])

    def close_export(self):
        button = self.find("Close")
        if button:
            self.click(button["key"])
            self.resync()

    def download(self, url: str, filename: str, directory: Path, budget: int, source_id: str) -> DownloadedFile:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "uarb.novascotia.ca" or not parsed.path.startswith("/fmi/webd/APP/connector/"):
            raise RetrievalError("Unexpected download origin")
        directory.mkdir(parents=True, exist_ok=True)
        filename = safe_filename(filename)
        target = directory / filename
        suffix = 2
        while target.exists():
            target = directory / f"{Path(filename).stem}-{suffix}{Path(filename).suffix}"
            suffix += 1
        partial = target.with_name(target.name + ".part")
        digest = hashlib.sha256()
        size = 0
        signature = b""
        try:
            with self.http.stream("GET", url, follow_redirects=False, timeout=httpx.Timeout(180, connect=15)) as response:
                response.raise_for_status()
                length_header = response.headers.get("content-length", "")
                declared = int(length_header) if length_header.isdigit() else None
                if declared is not None and declared > budget:
                    raise SizeLimitError("The requested files exceed the configured download-size budget")
                with partial.open("xb") as output:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > budget:
                            raise SizeLimitError("The requested files exceed the configured download-size budget")
                        if len(signature) < 12:
                            signature += chunk[:12 - len(signature)]
                        digest.update(chunk)
                        output.write(chunk)
                        if time.monotonic() - self.last_heartbeat > 25:
                            self.http.post(BASE + "HEARTBEAT/?v-uiId=" + str(self.ui_id)).raise_for_status()
                            self.last_heartbeat = time.monotonic()
                if not size:
                    raise RetrievalError("An exported file was empty")
                if declared is not None and not response.headers.get("content-encoding") and size != declared:
                    raise RetrievalError("An exported file was incomplete")
                if target.suffix.lower() == ".pdf" and not signature.startswith(b"%PDF-"):
                    raise RetrievalError("The website returned invalid PDF content")
                if signature.lstrip().lower().startswith((b"<!doctype", b"<html")) and target.suffix.lower() not in (".html", ".htm"):
                    raise RetrievalError("The website returned an HTML page instead of the requested file")
            partial.replace(target)
            return DownloadedFile(target.name, target, size, digest.hexdigest(), source_id)
        except BaseException:
            partial.unlink(missing_ok=True)
            raise


def retrieve(request: DocumentRequest, output_dir: Path, *, max_download_bytes: int = 100_000_000,
             workers: int = 1, client: httpx.Client | None = None) -> RetrievalResult:
    """Retrieve up to ten public files. No outgoing email and no browser launch.

    This first Python release deliberately supports one worker, the measured
    default. Initial returned rows are sufficient for tested matters; when they
    are insufficient we report the inventory limitation explicitly.
    """
    if workers != 1:
        raise ValueError("This Python release supports DOWNLOAD_WORKERS=1 only")
    if max_download_bytes <= 0:
        raise ValueError("Download budget must be positive")
    session = UarbClient(client)
    try:
        session.initialize()
        metadata, counts = session.search(request.matter_number)
        result = RetrievalResult(request.matter_number, request.document_type, metadata, counts)
        if counts[request.document_type] == 0:
            return result
        session.category(request.document_type)
        inventory = session.rows()
        selected = [row for row in inventory if row["public"]][:request.max_documents]
        if len(inventory) < counts[request.document_type] and len(selected) < request.max_documents:
            result.warnings.append("Only the initial returned document rows could be inspected; additional public files may exist. Pagination is not implemented in this release.")
        confidential = sum(not row["public"] for row in inventory)
        if confidential:
            result.warnings.append(f"Skipped {confidential} inspected entries without an explicit Public security label.")
        remaining = max_download_bytes
        for initial_row in selected:
            row = next((row for row in session.rows() if row["row"] == initial_row["row"] and row["labels"] == initial_row["labels"]), None)
            if row is None:
                raise RetrievalError("Document inventory changed while processing this request")
            url, filename = session.open_download(row)
            downloaded = session.download(url, filename, Path(output_dir), remaining,
                                          f"{request.matter_number}/{request.document_type}/row-{row['row']}")
            result.files.append(downloaded)
            remaining -= downloaded.size
            session.close_export()
        return result
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise RetrievalError("UARB retrieval failed; the public site may be unavailable or its protocol changed") from exc
    finally:
        session.close()
