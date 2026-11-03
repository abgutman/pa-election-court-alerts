#!/usr/bin/env python3
"""Pennsylvania election-litigation court alert monitor."""

from __future__ import annotations

import argparse
import hashlib
import html
import io
import json
import os
import re
import smtplib
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time as dt_time, timedelta, timezone
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import requests
from pypdf import PdfReader


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "watchlist.json"
STATE_PATH = ROOT / "data" / "state.json"
OUTPUT_JSON = ROOT / "output" / "latest.json"
OUTPUT_HTML = ROOT / "output" / "latest.html"
UA = "pa-election-court-alerts/1.0 (public-record monitor)"
ET_TZ = ZoneInfo("America/New_York")
UJS_BASE = "https://ujsportal.pacourts.us"
CL_API = "https://www.courtlistener.com/api/rest/v4"
COMMONWEALTH_COURT = "Pennsylvania Commonwealth Court"
TRACKED_COMMONWEALTH_SOURCE = "Commonwealth Court tracked docket"


@dataclass
class Item:
    source: str
    court: str
    source_id: str
    case_number: str
    title: str
    filed_at: str
    description: str
    url: str
    text: str
    precision: str = "date"
    matched_terms: list[str] = field(default_factory=list)
    priority: str = "standard"

    @property
    def fingerprint(self) -> str:
        stable = self.source_id or "|".join((self.case_number, self.filed_at, self.description, self.url))
        return hashlib.sha256(f"{self.source}|{self.court}|{stable}".encode()).hexdigest()


@dataclass
class Status:
    source: str
    court: str
    status: str
    fetched: int = 0
    inspected: int = 0
    detail: str = ""


@dataclass
class Collection:
    items: list[Item] = field(default_factory=list)
    observed: list[str] = field(default_factory=list)
    statuses: list[Status] = field(default_factory=list)


def load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def compact(value: Any) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def strip_tags(value: str) -> str:
    return compact(re.sub(r"<[^>]+>", " ", value or ""))


def normalize(value: str) -> str:
    value = (value or "").upper().replace("&", " AND ").replace("’", "'")
    return " ".join(re.findall(r"[A-Z0-9]+", value))


def watch_terms(config: dict[str, Any]) -> list[str]:
    terms: list[str] = []
    terms.extend(person["name"] for person in config["officials"])
    terms.extend(config["boards"])
    for candidate in config["candidates"]:
        terms.append(candidate["name"])
        terms.extend(candidate.get("aliases", []))
    terms.extend(config["campaign_committees"])
    terms.extend(config["political_parties"])
    terms.extend(config["pacs"])
    terms.extend(config["generic_keywords"])
    return list(dict.fromkeys(compact(term) for term in terms if compact(term)))


def matching_terms(text: str, terms: list[str]) -> list[str]:
    haystack = f" {normalize(text)} "
    matches = []
    for term in terms:
        needle = normalize(term)
        if needle and f" {needle} " in haystack:
            matches.append(term)
    return matches


def item_matching_terms(item: Item, terms: list[str]) -> list[str]:
    if item.source == TRACKED_COMMONWEALTH_SOURCE:
        return ["tracked election-related Commonwealth Court docket"]
    return matching_terms(item.text, terms)


def extract_pdf_text(content: bytes, max_pages: int | None = None) -> str:
    reader = PdfReader(io.BytesIO(content))
    pages = reader.pages if max_pages is None else reader.pages[:max_pages]
    return compact(" ".join((page.extract_text() or "") for page in pages))


def commonwealth_docket_entries(text: str) -> str:
    if "DOCKET ENTRY" not in text:
        return compact(text)
    section = text.rsplit("DOCKET ENTRY", 1)[-1]
    header = re.search(r"Filed Date\s+Docket Entry.*?Representing", section, re.S | re.I)
    if header:
        section = section[header.end():]
    section = re.sub(
        r"Neither the Appellate Courts nor the Administrative Office of Pennsylvania Courts.*?docket sheets\.",
        " ", section, flags=re.S | re.I,
    )
    section = re.sub(r"\b\d{1,2}:\d{2}\s+[AP]\.M\.", " ", section)
    section = re.sub(r"Commonwealth Court of PennsylvaniaCommonwealth Docket Sheet", " ", section, flags=re.I)
    section = re.sub(r"Docket Number:\s*[^\n]+", " ", section, flags=re.I)
    section = re.sub(r"Page\s+\d+\s+of\s+\d+", " ", section, flags=re.I)
    return compact(section)


def parse_since(raw: str | None, lookback_days: int) -> datetime:
    if not raw:
        return datetime.now(ET_TZ) - timedelta(days=lookback_days)
    value = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET_TZ)
    return parsed.astimezone(ET_TZ)


def item_is_since(item: Item, since: datetime) -> bool:
    raw = (item.filed_at or "").strip()
    if not raw:
        return True
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw) >= since.date()
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ET_TZ)
        return parsed.astimezone(ET_TZ) >= since
    except ValueError:
        return True


def iso_date(raw: str) -> str:
    token = compact(raw).split(" ")[0]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(token, fmt).date().isoformat()
        except ValueError:
            continue
    return token


def docket_from_title(title: str) -> str:
    match = re.search(r"\b\d+\s+[A-Z][A-Z.]{1,7}\s+\d{4}\b", title or "")
    return compact(match.group(0)) if match else ""


def request_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "*/*"})
    return session


FEDERAL = {
    "edpa": {
        "court": "U.S. District Court for the Eastern District of Pennsylvania",
        "rss": "https://ecf.paed.uscourts.gov/cgi-bin/rss_outside.pl",
        "cl": "paed",
    },
    "mdpa": {
        "court": "U.S. District Court for the Middle District of Pennsylvania",
        "rss": "https://ecf.pamd.uscourts.gov/cgi-bin/rss_outside.pl",
        "cl": "pamd",
    },
}


def collect_federal_rss(config: dict[str, Any], since: datetime) -> Collection:
    result = Collection()
    timeout = config["settings"]["request_timeout_seconds"]
    session = request_session()
    for key, spec in FEDERAL.items():
        try:
            response = session.get(spec["rss"], timeout=timeout)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            entries = root.findall(".//item")
            for node in entries:
                title = compact(node.findtext("title"))
                description = strip_tags(node.findtext("description") or "")
                link = compact(node.findtext("link"))
                guid = compact(node.findtext("guid")) or link or title
                pub_raw = compact(node.findtext("pubDate"))
                filed = ""
                if pub_raw:
                    try:
                        filed = parsedate_to_datetime(pub_raw).astimezone(ET_TZ).isoformat()
                    except (TypeError, ValueError):
                        filed = pub_raw
                docket_match = re.search(r"\b\d{1,2}:\d{2}-(?:cv|mc)-\d+\b", f"{title} {description} {link}", re.I)
                docket = docket_match.group(0) if docket_match else ""
                result.items.append(Item(
                    source=f"{key.upper()} official CM/ECF RSS",
                    court=spec["court"], source_id=guid, case_number=docket,
                    title=title, filed_at=filed, description=description,
                    url=link, text=f"{title} {description}", precision="time",
                ))
                result.observed.append(f"{key}:{guid}")
            result.statuses.append(Status(
                source="Official CM/ECF RSS", court=spec["court"], status="ok",
                fetched=len(entries), inspected=len(entries),
                detail="Official rolling feed; upstream normally retains roughly 24 hours of entries.",
            ))
        except Exception as error:  # noqa: BLE001
            result.statuses.append(Status(
                source="Official CM/ECF RSS", court=spec["court"], status="error",
                detail=f"{type(error).__name__}: {error}",
            ))
    return result


def _courtlistener_queries(config: dict[str, Any]) -> list[str]:
    generic = config["generic_keywords"]
    named = [person["name"] for person in config["officials"]]
    named += config["boards"]
    named += [candidate["name"] for candidate in config["candidates"]]
    named += config["campaign_committees"]
    groups = [generic]
    for start in range(0, len(named), 18):
        groups.append(named[start:start + 18])
    groups.append(config["political_parties"] + config["pacs"])
    output = []
    for group in groups:
        quoted = ['"' + value.replace('"', '\\"') + '"' for value in group]
        output.append(" OR ".join(quoted))
    return output


def collect_courtlistener(config: dict[str, Any], since: datetime) -> Collection:
    result = Collection()
    token = os.environ.get("CL_TOKEN", "").strip()
    headers = {"Authorization": f"Token {token}"} if token else {}
    timeout = config["settings"]["request_timeout_seconds"]
    session = request_session()
    failures: list[str] = []
    pages = 0
    fetched = 0
    courts = [
        ("paed", FEDERAL["edpa"]["court"]),
        ("pamd", FEDERAL["mdpa"]["court"]),
        ("ca3", "U.S. Court of Appeals for the Third Circuit"),
    ]
    for court_id, court_label in courts:
        for query_index, query in enumerate(_courtlistener_queries(config), start=1):
            params = {
                "type": "r", "court": court_id, "q": query,
                "filed_after": since.strftime("%m/%d/%Y"),
                "order_by": "dateFiled desc", "page_size": 100,
            }
            try:
                response = session.get(f"{CL_API}/search/", params=params, headers=headers, timeout=timeout)
                response.raise_for_status()
                payload = response.json()
                pages += 1
            except Exception as error:  # noqa: BLE001
                failures.append(f"{court_id} query {query_index}: {type(error).__name__}: {error}")
                continue
            hits = payload.get("results") or []
            fetched += len(hits)
            for hit in hits:
                docket_id = str(hit.get("docket_id") or hit.get("cluster_id") or "")
                case_name = compact(hit.get("caseName") or hit.get("case_name"))
                docket = compact(hit.get("docketNumber") or hit.get("docket_number"))
                filed = compact(hit.get("dateFiled") or hit.get("date_filed"))
                snippet = strip_tags(hit.get("snippet") or "")
                parties = " ".join(compact(value) for value in hit.get("party") or [])
                absolute = compact(hit.get("docket_absolute_url") or hit.get("absolute_url"))
                url = absolute if absolute.startswith("http") else f"https://www.courtlistener.com{absolute}"
                source_id = f"cl-case:{docket_id or hashlib.sha256((court_id + docket + case_name).encode()).hexdigest()}"
                text = f"{case_name} {docket} {snippet} {parties}"
                result.items.append(Item(
                    source="CourtListener RECAP search", court=court_label,
                    source_id=source_id, case_number=docket, title=case_name,
                    filed_at=iso_date(filed), description=snippet or "RECAP case/search match",
                    url=url, text=text,
                ))
                result.observed.append(f"{court_id}:{source_id}")
                for document in hit.get("recap_documents") or []:
                    document_id = str(document.get("id") or document.get("docket_entry_id") or document.get("pacer_doc_id") or "")
                    if not document_id:
                        document_id = hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()
                    description = compact(document.get("description") or document.get("short_description"))
                    doc_date = iso_date(compact(document.get("entry_date_filed") or document.get("date_filed") or filed))
                    doc_path = compact(document.get("absolute_url"))
                    doc_url = doc_path if doc_path.startswith("http") else f"https://www.courtlistener.com{doc_path}"
                    result.items.append(Item(
                        source="CourtListener RECAP search", court=court_label,
                        source_id=f"cl-document:{document_id}", case_number=docket,
                        title=case_name, filed_at=doc_date, description=description,
                        url=doc_url or url, text=f"{text} {description}",
                    ))
                    result.observed.append(f"{court_id}:cl-document:{document_id}")
    status = "error" if failures and pages == 0 else "partial" if failures else "ok"
    result.statuses.append(Status(
        source="CourtListener RECAP search", court="EDPA, MDPA, and Third Circuit",
        status=status, fetched=fetched, inspected=pages,
        detail="; ".join(failures[:4]) if failures else f"{pages} targeted search queries completed.",
    ))
    return result


APP_COURTS = {
    "Supreme": "Pennsylvania Supreme Court",
    "Superior": "Pennsylvania Superior Court",
    "Commonwealth": COMMONWEALTH_COURT,
}
APP_RSS = {
    "Pennsylvania Supreme Court": "https://www.pacourts.us/Rss/Opinions/Supreme/",
    "Pennsylvania Superior Court": "https://www.pacourts.us/Rss/Opinions/Superior/",
    COMMONWEALTH_COURT: "https://www.pacourts.us/Rss/Opinions/Commonwealth/",
}


def _parse_ujs_results(page: str, court: str) -> list[Item]:
    output: list[Item] = []
    start = page.find("<tbody")
    if start < 0:
        return output
    end = page.find("</tbody>", start)
    table = page[start:end] if end > start else page[start:]
    for row_match in re.finditer(r"<tr>(.*?)</tr>", table, re.S):
        row = row_match.group(1)
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 7:
            continue
        docket = strip_tags(cells[2])
        title = strip_tags(cells[4])
        status = strip_tags(cells[5])
        filed = iso_date(strip_tags(cells[6]))
        link_match = re.search(r'href=["\'/]*(Report/PacDocketSheet\?docketNumber=[^"\'&]+&amp;dnh=[^"\']+)', row)
        url = f"{UJS_BASE}/{html.unescape(link_match.group(1))}" if link_match else ""
        output.append(Item(
            source="UJS appellate case search", court=court,
            source_id=f"ujs:{court}:{docket}", case_number=docket, title=title,
            filed_at=filed, description=status, url=url,
            text=f"{title} {status} {docket}",
        ))
    return output


def collect_pa_appellate(config: dict[str, Any], since: datetime, source_seen: set[str]) -> Collection:
    result = Collection()
    timeout = config["settings"]["request_timeout_seconds"]
    session = request_session()
    try:
        page = session.get(f"{UJS_BASE}/CaseSearch", timeout=timeout)
        page.raise_for_status()
        token_match = re.search(r'__RequestVerificationToken.*?value=["\']([^"\']+)', page.text)
        token = token_match.group(1) if token_match else ""
        for key, court in APP_COURTS.items():
            pdf_failures: list[str] = []
            pdfs_inspected = 0
            payload = {
                "SearchBy": "AppellateCourtName", "AppellateCourtName": key,
                "FiledStartDate": since.date().isoformat(),
                "FiledEndDate": date.today().isoformat(),
                "__RequestVerificationToken": token,
            }
            response = session.post(f"{UJS_BASE}/CaseSearch", data=payload, headers={"Referer": f"{UJS_BASE}/CaseSearch"}, timeout=timeout)
            response.raise_for_status()
            items = _parse_ujs_results(response.text, court)
            if court == COMMONWEALTH_COURT:
                for item in items:
                    item.priority = "high"
                    if not item.url or item.source_id in source_seen:
                        continue
                    try:
                        docket_response = session.get(item.url, timeout=timeout)
                        docket_response.raise_for_status()
                        item.text = f"{item.text} {extract_pdf_text(docket_response.content)}"
                        pdfs_inspected += 1
                    except Exception as error:  # noqa: BLE001
                        pdf_failures.append(f"{item.case_number}: {type(error).__name__}: {error}")
            result.items.extend(items)
            result.observed.extend(item.source_id for item in items)
            result.statuses.append(Status(
                source="UJS appellate case search", court=court,
                status="partial" if pdf_failures else "ok",
                fetched=len(items), inspected=len(items),
                detail=("; ".join(pdf_failures[:3]) if pdf_failures else
                        f"New appellate dockets searched from {since.date().isoformat()}; "
                        f"{pdfs_inspected} new Commonwealth docket PDF(s) text-checked."),
            ))
    except Exception as error:  # noqa: BLE001
        result.statuses.append(Status(
            source="UJS appellate case search", court="Pennsylvania appellate courts",
            status="error", detail=f"{type(error).__name__}: {error}",
        ))

    for court, url in APP_RSS.items():
        pdf_failures: list[str] = []
        pdfs_inspected = 0
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            root = ET.fromstring(response.content)
            entries = root.findall(".//item")
            for node in entries:
                title = compact(node.findtext("title"))
                link = compact(node.findtext("link"))
                guid = compact(node.findtext("guid")) or link
                pub = compact(node.findtext("pubDate"))
                filed = ""
                if pub:
                    try:
                        filed = parsedate_to_datetime(pub).astimezone(ET_TZ).isoformat()
                    except (TypeError, ValueError):
                        filed = pub
                source_id = f"opinion:{guid}"
                text = title
                if court == COMMONWEALTH_COURT and source_id not in source_seen and link:
                    try:
                        opinion_response = session.get(link, timeout=timeout)
                        opinion_response.raise_for_status()
                        text = f"{title} {extract_pdf_text(opinion_response.content, max_pages=12)}"
                        pdfs_inspected += 1
                    except Exception as error:  # noqa: BLE001
                        pdf_failures.append(f"{title}: {type(error).__name__}: {error}")
                item = Item(
                    source="Pennsylvania Courts opinion RSS", court=court,
                    source_id=source_id, case_number=docket_from_title(title), title=title,
                    filed_at=filed, description="Published opinion", url=link,
                    text=text, precision="time",
                    priority="high" if court == COMMONWEALTH_COURT else "standard",
                )
                result.items.append(item)
                result.observed.append(item.source_id)
            result.statuses.append(Status(
                source="Pennsylvania Courts opinion RSS", court=court,
                status="partial" if pdf_failures else "ok",
                fetched=len(entries), inspected=len(entries),
                detail=("; ".join(pdf_failures[:3]) if pdf_failures else
                        f"Official published-opinion feed; {pdfs_inspected} new Commonwealth opinion PDF(s) text-checked."),
            ))
        except Exception as error:  # noqa: BLE001
            result.statuses.append(Status(
                source="Pennsylvania Courts opinion RSS", court=court, status="error",
                detail=f"{type(error).__name__}: {error}",
            ))
    return result


def collect_tracked_commonwealth_dockets(
    config: dict[str, Any], state: dict[str, Any], update_state: bool,
) -> Collection:
    result = Collection()
    tracked = state.get("tracked_commonwealth_dockets", {})
    if not tracked:
        result.statuses.append(Status(
            source=TRACKED_COMMONWEALTH_SOURCE, court=COMMONWEALTH_COURT, status="ok",
            detail="No election-related Commonwealth Court dockets are tracked yet.",
        ))
        return result
    timeout = config["settings"]["request_timeout_seconds"]
    session = request_session()
    failures: list[str] = []
    inspected = 0
    for docket, metadata in tracked.items():
        url = compact(metadata.get("url"))
        if not url:
            failures.append(f"{docket}: missing docket URL")
            continue
        try:
            response = session.get(url, timeout=timeout)
            response.raise_for_status()
            entries = commonwealth_docket_entries(extract_pdf_text(response.content))
            digest = hashlib.sha256(entries.encode()).hexdigest()
            previous = compact(metadata.get("entries_hash"))
            inspected += 1
            if previous and digest != previous:
                result.items.append(Item(
                    source=TRACKED_COMMONWEALTH_SOURCE, court=COMMONWEALTH_COURT,
                    source_id=f"commonwealth-docket:{docket}:{digest}", case_number=docket,
                    title=compact(metadata.get("title")) or docket,
                    filed_at=date.today().isoformat(),
                    description="The official Commonwealth Court docket sheet changed. Open the docket for the newest filing or order.",
                    url=url, text=entries, priority="high",
                ))
            if update_state:
                metadata["entries_hash"] = digest
        except Exception as error:  # noqa: BLE001
            failures.append(f"{docket}: {type(error).__name__}: {error}")
    result.statuses.append(Status(
        source=TRACKED_COMMONWEALTH_SOURCE, court=COMMONWEALTH_COURT,
        status="partial" if failures and inspected else "error" if failures else "ok",
        fetched=len(tracked), inspected=inspected,
        detail="; ".join(failures[:3]) if failures else f"Polled {inspected} tracked election-related docket sheet(s).",
    ))
    return result


def collect_philadelphia(config: dict[str, Any], since: datetime) -> Collection:
    result = Collection()
    court = "Philadelphia Court of Common Pleas"
    url = "https://raw.githubusercontent.com/abgutman/av-tools/main/ccp_dockets/data/complaints.json"
    try:
        response = request_session().get(url, timeout=config["settings"]["request_timeout_seconds"])
        response.raise_for_status()
        cases = response.json()
        for case in cases:
            filed = iso_date(case.get("filing_date", ""))
            if filed and date.fromisoformat(filed) < since.date():
                continue
            case_id = compact(case.get("case_id"))
            parties = " ".join(compact(party.get("name")) for party in case.get("parties", []))
            case_type = compact(case.get("case_type"))
            title = compact(case.get("caption") or parties)
            item = Item(
                source="Philadelphia FJD complaints mirror", court=court,
                source_id=f"phila:{case_id}", case_number=case_id, title=title,
                filed_at=filed, description=case_type,
                url=f"https://fjdefile.phila.gov/efsfjd/zk_fjd_public_qry_00.zp_main_idx",
                text=f"{title} {parties} {case_type}",
            )
            result.items.append(item)
            result.observed.append(item.source_id)
        newest = max((iso_date(case.get("filing_date", "")) for case in cases if case.get("filing_date")), default="unknown")
        result.statuses.append(Status(
            source="Philadelphia FJD complaints mirror", court=court, status="ok",
            fetched=len(cases), inspected=len(result.items),
            detail=f"Periodic public-data mirror; newest filing date in source is {newest}.",
        ))
    except Exception as error:  # noqa: BLE001
        result.statuses.append(Status(
            source="Philadelphia FJD complaints mirror", court=court, status="error",
            detail=f"{type(error).__name__}: {error}",
        ))
    return result


PSI = {
    "bucks": ("https://propublic.buckscountyonline.org", "Bucks County Court of Common Pleas"),
    "montgomery": ("https://courtsapp.montcopa.org", "Montgomery County Court of Common Pleas"),
}
PSI_GRID = re.compile(r"/v/detail/Case/(\d+)['\"]?[^>]*>\s*Select\s*</a>.*?<td>\s*(20\d{2}-\d+)\s*</td>", re.I | re.S)


def _psi_search_url(base: str, start: str, end: str, count: int, skip: int) -> str:
    params = {
        "Q": "", "IncludeSoundsLike": "false", "Count": str(count), "fromAdv": "1",
        "CaseNumber": "", "ParcelNumber": "", "CaseType": "", "DateCommencedFrom": start,
        "DateCommencedTo": end, "IncludeInitialFilings": "false", "IncludeInitialEFilings": "false",
        "FilingType": "", "FilingDateFrom": "", "FilingDateTo": "",
        "IncludeSubsequentFilings": "false", "IncludeSubsequentEFilings": "false",
        "Court": "C", "JudgeID": "", "Attorney": "", "AttorneyID": "",
        "Grid": "true", "Sort": "DateCommenced desc", "Skip": str(skip),
    }
    return f"{base}/psi/v/search/case?{urlencode(params, quote_via=quote)}"


def _psi_field(markup: str, label: str) -> str:
    match = re.search(re.escape(label) + r"\s*</[^>]+>\s*<[^>]+>\s*([^<]+)", markup or "", re.I)
    return compact(match.group(1)) if match else ""


def _psi_parties(data: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for markup in data.get("Relates", []):
        for row in re.finditer(r"<tr[^>]*>(.*?)</tr>", markup, re.S | re.I):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row.group(1), re.S | re.I)
            for cell in cells:
                if "Select" in cell or "noprint" in cell:
                    continue
                name = strip_tags(cell)
                name = re.split(r"\s*\(", name)[0].strip()
                if name and name.lower() != "name":
                    names.append(name)
                    break
    return list(dict.fromkeys(names))


def collect_psi(config: dict[str, Any], since: datetime, source_seen: set[str]) -> Collection:
    result = Collection()
    timeout = config["settings"]["request_timeout_seconds"]
    delay = config["settings"]["request_delay_seconds"]
    for key, (base, court) in PSI.items():
        session = request_session()
        failures: list[str] = []
        try:
            bootstrap = session.get(f"{base}/psi/v/search/case", timeout=timeout)
            body = bootstrap.text or ""
            final_url = bootstrap.url.lower()
            if "cf-turnstile" in body or "/auth/init" in final_url:
                raise RuntimeError("viewer is gated by a CAPTCHA/Turnstile challenge")
            start = since.strftime("%m/%d/%Y")
            end = datetime.now(ET_TZ).strftime("%m/%d/%Y")
            stubs: list[tuple[str, str]] = []
            skip = 0
            while True:
                response = session.get(_psi_search_url(base, start, end, 100, skip), timeout=max(timeout, 90))
                response.raise_for_status()
                rows = PSI_GRID.findall(response.text)
                stubs.extend(rows)
                if len(rows) < 100:
                    break
                skip += 100
            deduped = list(dict.fromkeys(stubs))
            for internal_id, case_number in deduped:
                observed_id = f"{key}:{case_number}"
                result.observed.append(observed_id)
                if observed_id in source_seen:
                    continue
                try:
                    if delay:
                        time.sleep(delay)
                    detail = session.post(f"{base}/psi/v/detail/Case/{internal_id}/data", json={"DocketRange": "100"}, timeout=timeout)
                    detail.raise_for_status()
                    data = detail.json()
                    markup = data.get("Detail", "")
                    case_type = _psi_field(markup, "Case Type")
                    plaintiff = _psi_field(markup, "Caption Plaintiff")
                    defendant = _psi_field(markup, "Caption Defendant")
                    title = f"{plaintiff or '—'} v. {defendant or '—'}"
                    parties = _psi_parties(data)
                    filed = iso_date(_psi_field(markup, "Commencement Date"))
                    result.items.append(Item(
                        source=f"{court} PSI Web Viewer", court=court,
                        source_id=observed_id, case_number=case_number, title=title,
                        filed_at=filed, description=case_type,
                        url=f"{base}/psi/v/detail/Case/{internal_id}",
                        text=f"{title} {' '.join(parties)} {case_type}",
                    ))
                except Exception as error:  # noqa: BLE001
                    failures.append(f"{case_number}: {type(error).__name__}: {error}")
            result.statuses.append(Status(
                source="PSI Web Viewer", court=court,
                status="partial" if failures else "ok", fetched=len(deduped),
                inspected=sum(1 for value in result.observed if value.startswith(key + ":") and value not in source_seen),
                detail="; ".join(failures[:3]) if failures else f"Civil cases opened from {start} through {end}.",
            ))
        except Exception as error:  # noqa: BLE001
            result.statuses.append(Status(
                source="PSI Web Viewer", court=court, status="error",
                detail=f"{type(error).__name__}: {error}",
            ))
    return result


CTRACK = {
    "delaware": {
        "court": "Delaware County Court of Common Pleas",
        "api": "https://delcopublicaccessapi.co.delaware.pa.us/api/v1",
        "web": "https://delcopublicaccess.co.delaware.pa.us",
    },
    "monroe": {
        "court": "Monroe County Court of Common Pleas",
        "api": "https://public-prod.43rdjdpacivilcms.us/api/v1",
        "web": "https://public-prod.43rdjdpacivilcms.us",
    },
}


def collect_ctrack(config: dict[str, Any], since: datetime, source_seen: set[str]) -> Collection:
    result = Collection()
    timeout = config["settings"]["request_timeout_seconds"]
    delay = config["settings"]["request_delay_seconds"]
    for key, spec in CTRACK.items():
        session = request_session()
        failures: list[str] = []
        stubs: list[dict[str, Any]] = []
        try:
            page = 1
            while True:
                params = {
                    "queryString": "true",
                    "searchFields[0].searchType": "", "searchFields[0].operation": ">=",
                    "searchFields[0].values[0]": since.date().isoformat(), "searchFields[0].indexFieldName": "filedDate",
                    "searchFields[1].searchType": "", "searchFields[1].operation": "<=",
                    "searchFields[1].values[0]": date.today().isoformat(), "searchFields[1].indexFieldName": "filedDate",
                    "page": str(page), "pageSize": "100", "sortField": "filedDate", "sortDirection": "desc",
                }
                response = session.get(f"{spec['api']}/cases/search", params=params, headers={"Origin": spec["web"], "Referer": f"{spec['web']}/"}, timeout=timeout)
                response.raise_for_status()
                payload = response.json()
                batch = payload.get("resultItems") or []
                stubs.extend(batch)
                if len(batch) < 100:
                    break
                page += 1
            for entry in stubs:
                row = entry.get("rowMap") or entry
                case_id = compact(row.get("caseID") or (row.get("caseHeader") or {}).get("caseID"))
                case_number = compact(row.get("caseNumber") or (row.get("caseHeader") or {}).get("caseNumber"))
                observed_id = f"{key}:{case_id or case_number}"
                result.observed.append(observed_id)
                if observed_id in source_seen:
                    continue
                title = compact(row.get("shortTitle") or (row.get("caseHeader") or {}).get("shortTitle"))
                case_type = compact(row.get("caseClassification") or row.get("caseType"))
                filed = iso_date(compact(row.get("filedDate") or (row.get("caseHeader") or {}).get("filedDate")))
                parties: list[str] = []
                if case_id:
                    try:
                        if delay:
                            time.sleep(delay)
                        party_response = session.get(f"{spec['api']}/cases/{case_id}/parties", headers={"Origin": spec["web"], "Referer": f"{spec['web']}/"}, timeout=timeout)
                        party_response.raise_for_status()
                        raw_parties = party_response.json()
                        for group in raw_parties:
                            for party in group if isinstance(group, list) else [group]:
                                info = party.get("partyName") or {}
                                name = compact(info.get("displayName") or info.get("sortName"))
                                if name:
                                    parties.append(name)
                    except Exception as error:  # noqa: BLE001
                        failures.append(f"parties {case_number}: {type(error).__name__}: {error}")
                result.items.append(Item(
                    source=f"{key.title()} C-Track Public Access", court=spec["court"],
                    source_id=observed_id, case_number=case_number, title=title,
                    filed_at=filed, description=case_type,
                    url=f"{spec['web']}/#/cases/{case_id}" if case_id else spec["web"],
                    text=f"{title} {' '.join(parties)} {case_type}",
                ))
            result.statuses.append(Status(
                source="C-Track Public Access", court=spec["court"],
                status="partial" if failures else "ok", fetched=len(stubs),
                inspected=sum(1 for value in result.observed if value.startswith(key + ":") and value not in source_seen),
                detail="; ".join(failures[:3]) if failures else f"Civil cases opened since {since.date().isoformat()}.",
            ))
        except Exception as error:  # noqa: BLE001
            result.statuses.append(Status(
                source="C-Track Public Access", court=spec["court"], status="error",
                detail=f"{type(error).__name__}: {error}",
            ))
    return result


def unavailable_statuses(config: dict[str, Any]) -> list[Status]:
    output = []
    for court in config["courts"]:
        if court["coverage"] in {"subscription_required", "manual_or_account_required"}:
            output.append(Status(
                source=court["source"], court=court["name"], status="unavailable",
                detail=court.get("note") or "Scoped court, but no dependable anonymous bulk filing feed is available to this repository.",
            ))
    return output


def render_report(report: dict[str, Any]) -> tuple[str, str]:
    lines = [
        f"PA election-court alert test/run",
        f"Window: {report['since']} through {report['generated_at']}",
        f"Matches: {len(report['items'])}",
        "",
    ]
    if report["items"]:
        for item in report["items"]:
            priority = "HIGH PRIORITY — " if item.get("priority") == "high" else ""
            lines.extend([
                f"{priority}{item['court']} — {item['case_number'] or 'no docket number'}",
                item["title"] or "(no title)",
                f"Filed: {item['filed_at'] or 'unknown'} ({item['precision']}-level source)",
                f"Matched: {', '.join(item['matched_terms'])}",
                item["description"], item["url"], "",
            ])
    else:
        lines.extend(["No matching filings were found in the reachable sources.", ""])
    lines.append("Source health:")
    for status in report["statuses"]:
        lines.append(f"- {status['status'].upper()} | {status['court']} | {status['source']} | {status['detail']}")
    lines.extend([
        "", "Timing note: county and UJS sources often expose filing dates without times. For the Nov. 2 8:00 a.m. test, all matching Nov. 2 date-only records are included conservatively.",
    ])
    text = "\n".join(lines)

    item_cards = "".join(
        ("<article class=\"high\"><p><b>HIGH PRIORITY — COMMONWEALTH COURT</b></p>" if item.get("priority") == "high" else "<article>")
        + "<h3>" + html.escape(item["title"] or "(no title)") + "</h3>"
        + f"<p><b>{html.escape(item['court'])}</b> · {html.escape(item['case_number'] or 'no docket')} · {html.escape(item['filed_at'] or 'unknown date')}</p>"
        + f"<p>{html.escape(item['description'])}</p>"
        + f"<p>Matched: {html.escape(', '.join(item['matched_terms']))}</p>"
        + (f"<p><a href=\"{html.escape(item['url'], quote=True)}\">Open record</a></p>" if item["url"] else "")
        + "</article>"
        for item in report["items"]
    ) or "<p>No matching filings were found in the reachable sources.</p>"
    rows = "".join(
        f"<tr><td>{html.escape(status['status'])}</td><td>{html.escape(status['court'])}</td><td>{html.escape(status['source'])}</td><td>{html.escape(status['detail'])}</td></tr>"
        for status in report["statuses"]
    )
    body = f"""<!doctype html><html><head><meta charset=\"utf-8\"><title>PA election-court alerts</title>
<style>body{{font:15px/1.45 system-ui;margin:0;background:#f4f6f8;color:#17202a}}main{{max-width:980px;margin:auto;padding:28px}}article,section{{background:white;padding:18px;margin:14px 0;border-radius:8px}}article.high{{border-left:6px solid #b42318}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{border:1px solid #d5d8dc;padding:7px;text-align:left;vertical-align:top}}h1{{color:#17365d}}a{{color:#1756a9}}</style></head><body><main>
<h1>PA election-court alert test/run</h1><p>Window: {html.escape(report['since'])} through {html.escape(report['generated_at'])}</p><p><b>{len(report['items'])} matching filing(s)</b></p>
{item_cards}<section><h2>Source health</h2><table><thead><tr><th>Status</th><th>Court</th><th>Source</th><th>Detail</th></tr></thead><tbody>{rows}</tbody></table></section>
<section><b>Timing note:</b> County and UJS sources often expose filing dates without times. For the Nov. 2 8:00 a.m. test, all matching Nov. 2 date-only records are included conservatively.</section>
</main></body></html>"""
    return text, body


def send_email(subject: str, text: str, body: str) -> None:
    username = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    recipient = os.environ.get("ALERT_TO", "").strip()
    if not username or not password or not recipient:
        raise RuntimeError("GMAIL_USER, GMAIL_APP_PASSWORD, and ALERT_TO are required")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = username
    message["To"] = recipient
    message.set_content(text)
    message.add_alternative(body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(username, password)
        smtp.send_message(message)


def combine(collections: list[Collection]) -> Collection:
    output = Collection()
    for collection in collections:
        output.items.extend(collection.items)
        output.observed.extend(collection.observed)
        output.statuses.extend(collection.statuses)
    deduped: dict[str, Item] = {}
    for item in output.items:
        deduped[item.fingerprint] = item
    output.items = list(deduped.values())
    output.observed = list(dict.fromkeys(output.observed))
    return output


def public_item(item: Item) -> dict[str, Any]:
    output = asdict(item)
    output.pop("text", None)
    output["fingerprint"] = item.fingerprint
    return output


def run(args: argparse.Namespace) -> int:
    config = load_json(CONFIG_PATH)
    state = load_json(STATE_PATH) if STATE_PATH.exists() else {"version": 1, "seen": {}, "last_run": None}
    since = parse_since(args.since, int(config["settings"]["lookback_days"]))
    terms = watch_terms(config)
    all_seen = set(value for values in state.get("seen", {}).values() for value in values)

    collectors: list[Callable[[], Collection]] = [
        lambda: collect_federal_rss(config, since),
        lambda: collect_courtlistener(config, since),
        lambda: collect_pa_appellate(config, since, all_seen),
        lambda: collect_tracked_commonwealth_dockets(config, state, not args.no_state),
        lambda: collect_philadelphia(config, since),
        lambda: collect_psi(config, since, all_seen),
        lambda: collect_ctrack(config, since, all_seen),
    ]
    collections: list[Collection] = []
    for collector in collectors:
        try:
            collections.append(collector())
        except Exception as error:  # noqa: BLE001
            collections.append(Collection(statuses=[Status(
                source=getattr(collector, "__name__", "collector"), court="unknown",
                status="error", detail=f"Unhandled {type(error).__name__}: {error}",
            )]))
    collected = combine(collections)
    collected.statuses.extend(unavailable_statuses(config))

    matches: list[Item] = []
    existing_fingerprints = set(state.get("seen", {}).get("alerts", []))
    for item in collected.items:
        if not item_is_since(item, since):
            continue
        item.matched_terms = item_matching_terms(item, terms)
        if not item.matched_terms:
            continue
        if not args.replay and item.fingerprint in existing_fingerprints:
            continue
        matches.append(item)
    matches.sort(
        key=lambda item: (item.priority == "high", item.filed_at, item.court, item.case_number),
        reverse=True,
    )

    if not args.no_state:
        tracked = state.setdefault("tracked_commonwealth_dockets", {})
        for item in matches:
            if item.court != COMMONWEALTH_COURT or item.source != "UJS appellate case search" or not item.url:
                continue
            tracked.setdefault(item.case_number, {
                "title": item.title,
                "url": item.url,
                "tracked_at": datetime.now(ET_TZ).isoformat(),
                "entries_hash": "",
            })

    generated = datetime.now(ET_TZ).isoformat()
    report = {
        "generated_at": generated,
        "since": since.isoformat(),
        "scheduled": args.scheduled,
        "items": [public_item(item) for item in matches],
        "statuses": [asdict(status) for status in collected.statuses],
        "watchlist": {
            "terms": len(terms), "officials": len(config["officials"]),
            "boards": len(config["boards"]), "parties": len(config["political_parties"]),
            "pacs": len(config["pacs"]), "candidates": len(config["candidates"]),
        },
    }
    current_health_errors = {
        f"{status.court}|{status.source}": f"{status.status}|{status.detail}"
        for status in collected.statuses if status.status == "error"
    }
    previous_health_errors = state.get("health_errors", {})
    health_changed = current_health_errors != previous_health_errors
    text_report, html_report = render_report(report)
    atomic_write(OUTPUT_JSON, json.dumps(report, indent=2, sort_keys=True) + "\n")
    atomic_write(OUTPUT_HTML, html_report)

    if not args.no_state:
        limit = int(config["settings"]["state_limit_per_source"])
        seen = state.setdefault("seen", {})
        for observed in collected.observed:
            source = observed.split(":", 1)[0]
            bucket = seen.setdefault(source, [])
            if observed not in bucket:
                bucket.append(observed)
            if len(bucket) > limit:
                del bucket[:-limit]
        alerts = seen.setdefault("alerts", [])
        for item in matches:
            if item.fingerprint not in alerts:
                alerts.append(item.fingerprint)
        if len(alerts) > limit:
            del alerts[:-limit]
        state["last_run"] = generated
        state["health_errors"] = current_health_errors
        atomic_write(STATE_PATH, json.dumps(state, indent=2, sort_keys=True) + "\n")

    print(text_report)
    should_send = args.test or bool(matches) or health_changed
    if args.send and should_send:
        prefix = "TEST — " if args.test else ""
        high_priority = sum(item.priority == "high" for item in matches)
        subject = (
            f"{prefix}PA election-court alerts: {len(matches)} match(es), {high_priority} high-priority, "
            f"{len(current_health_errors)} source error(s) since {since.strftime('%b. %-d, %-I:%M %p')}"
        )
        send_email(subject, text_report, html_report)
        print("Email sent.")
    elif args.send:
        print("No new matches or changed source failures; no email sent.")
    return 2 if any(status.status == "error" for status in collected.statuses) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="ISO timestamp; naïve values are interpreted in America/New_York")
    parser.add_argument("--send", action="store_true", help="send the report by email")
    parser.add_argument("--test", action="store_true", help="mark the email subject as a test")
    parser.add_argument("--replay", action="store_true", help="include matches already recorded in state")
    parser.add_argument("--no-state", action="store_true", help="do not update deduplication state")
    parser.add_argument("--scheduled", action="store_true", help="mark the report as a scheduled run")
    return run(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
