"""Fetch provider status pages and normalise them into one Incident shape.

Every adapter returns a list of Incident. Adapters raise FetchError on any
transport or parse problem; the caller treats that as "no information from this
provider this run" and must never interpret it as a resolution.
"""

import gzip
import html
import io
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

USER_AGENT = (
    "Mozilla/5.0 (compatible; ai-bat-phone/1.0; "
    "+https://github.com/Sephatron/ai-bat-phone)"
)
TIMEOUT = 20

# Ordered worst-last so a numeric comparison detects escalation.
IMPACT_RANK = {"none": 0, "maintenance": 0, "minor": 1, "major": 2, "critical": 3}

# Incident lifecycle, in the order Statuspage advances through it.
INCIDENT_STATUSES = ("investigating", "identified", "monitoring", "resolved", "postmortem")
MAINTENANCE_STATUSES = ("scheduled", "in_progress", "verifying", "completed")


class FetchError(Exception):
    """A provider could not be read this run. Never a signal about their uptime."""


@dataclass
class Incident:
    provider_key: str
    provider_name: str
    incident_id: str
    title: str
    status: str
    impact: str
    kind: str  # "incident" | "maintenance"
    url: str
    started_at: datetime
    updated_at: datetime
    components: list = field(default_factory=list)
    latest_update: str = ""

    @property
    def key(self):
        return "%s:%s" % (self.provider_key, self.incident_id)

    @property
    def is_over(self):
        return self.status in ("resolved", "postmortem", "completed")


def _get(url, accept):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": accept, "Accept-Encoding": "gzip"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            return resp.status, raw
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except Exception as exc:  # socket errors, DNS, TLS, redirects loops
        raise FetchError("%s: %s" % (url, exc))


def _get_json(url):
    status, raw = _get(url, "application/json")
    if status != 200:
        raise FetchError("%s returned HTTP %s" % (url, status))
    try:
        return json.loads(raw)
    except ValueError:
        # Several status hosts answer 200 with an HTML shell for unknown paths.
        raise FetchError("%s returned 200 but not JSON" % url)


def _parse_iso(value):
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _strip_html(value):
    if not value:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", value)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def _truncate(text, limit=600):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# --------------------------------------------------------------------------
# statuspage: Atlassian Statuspage, Instatus and incident.io all serve this.
# --------------------------------------------------------------------------

def fetch_statuspage(provider):
    base = provider["base"].rstrip("/")
    out = []
    payload = _get_json(base + "/api/v2/incidents.json")
    for raw in payload.get("incidents", []):
        incident = _statuspage_incident(provider, raw, base)
        if incident:
            out.append(incident)

    # Only Atlassian serves this one; Instatus and incident.io answer 404. A
    # missing maintenance endpoint is normal, so it must not fail the provider.
    try:
        maint = _get_json(base + "/api/v2/scheduled-maintenances.json")
    except FetchError:
        maint = None
    if maint:
        for raw in maint.get("scheduled_maintenances", []):
            incident = _statuspage_incident(provider, raw, base, kind="maintenance")
            if incident:
                out.append(incident)
    return out


def _statuspage_incident(provider, raw, base, kind="incident"):
    incident_id = raw.get("id")
    if not incident_id:
        return None
    started = _parse_iso(raw.get("started_at") or raw.get("created_at") or raw.get("scheduled_for"))
    updated = _parse_iso(raw.get("updated_at")) or started
    if not started:
        return None
    updates = raw.get("incident_updates") or []
    body = _strip_html(updates[0].get("body", "")) if updates else ""
    status = (raw.get("status") or "").lower()
    if kind == "maintenance" and status not in MAINTENANCE_STATUSES:
        status = "scheduled"
    impact = (raw.get("impact") or "none").lower()
    if impact not in IMPACT_RANK:
        impact = "none"
    return Incident(
        provider_key=provider["key"],
        provider_name=provider["name"],
        incident_id=str(incident_id),
        title=raw.get("name") or "(untitled incident)",
        status=status,
        impact=impact,
        kind=kind,
        url=raw.get("shortlink") or "%s/incidents/%s" % (base, incident_id),
        started_at=started,
        updated_at=updated or started,
        components=[c.get("name") for c in raw.get("components", []) if c.get("name")],
        latest_update=_truncate(body),
    )


# --------------------------------------------------------------------------
# rss: history.rss, for pages that block JSON but publish a feed.
# --------------------------------------------------------------------------

# History feeds label each entry in one of three ways. In rough order of how
# reliable they are: a bracketed marker at the head of the body ("[Resolved] …",
# Rootly and Statuspage), an explicit "Status: resolved" line (DeepSeek), or a
# "Type: Incident / Duration: 40 minutes" preamble (Perplexity) where the
# presence of a duration is what tells you the thing is over.
_RSS_MARKER = re.compile(r"^\s*\[([A-Za-z ]{3,20})\]")
_RSS_STATUS_LINE = re.compile(r"\bstatus:\s*([a-z_ ]+)", re.I)
_RSS_TYPE_LINE = re.compile(r"\btype:\s*(incident|maintenance)\b", re.I)
_RSS_DURATION = re.compile(r"\bduration:\s*\d", re.I)

_MARKER_TO_STATUS = {
    "resolved": ("incident", "resolved"),
    "postmortem": ("incident", "postmortem"),
    "investigating": ("incident", "investigating"),
    "identified": ("incident", "identified"),
    "monitoring": ("incident", "monitoring"),
    "update": ("incident", "monitoring"),
    "scheduled": ("maintenance", "scheduled"),
    "in_progress": ("maintenance", "in_progress"),
    "verifying": ("maintenance", "verifying"),
    "completed": ("maintenance", "completed"),
    "maintenance": ("maintenance", "scheduled"),
}


def _rss_classify(text):
    """Work out (kind, status) for one history-feed entry."""
    marker = _RSS_MARKER.match(text)
    if marker:
        word = marker.group(1).strip().lower().replace(" ", "_")
        if word in _MARKER_TO_STATUS:
            return _MARKER_TO_STATUS[word]

    line = _RSS_STATUS_LINE.search(text)
    if line:
        word = line.group(1).strip().lower().replace(" ", "_")
        # "Status: resolved and monitoring" — take the leading word only.
        word = word.split("_")[0] if word not in _MARKER_TO_STATUS else word
        if word in _MARKER_TO_STATUS:
            return _MARKER_TO_STATUS[word]

    type_line = _RSS_TYPE_LINE.search(text)
    kind = "maintenance" if type_line and type_line.group(1).lower() == "maintenance" else "incident"
    if _RSS_DURATION.search(text):
        # A published duration means the provider has closed it out.
        return kind, ("completed" if kind == "maintenance" else "resolved")

    if _looks_resolved(text):
        return kind, ("completed" if kind == "maintenance" else "resolved")
    return kind, ("scheduled" if kind == "maintenance" else "investigating")


def fetch_rss(provider):
    import xml.etree.ElementTree as ET

    base = provider["base"].rstrip("/")
    status, raw = _get(base + "/history.rss", "application/rss+xml, application/xml")
    if status != 200:
        raise FetchError("%s/history.rss returned HTTP %s" % (base, status))
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise FetchError("%s/history.rss is not valid XML: %s" % (base, exc))

    out = []
    for item in root.iterfind(".//channel/item"):
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        if not guid:
            continue
        title = (item.findtext("title") or "(untitled incident)").strip()
        description_raw = item.findtext("description") or ""
        description = _strip_html(description_raw)
        when = _parse_rfc822(item.findtext("pubDate"))
        if not when:
            continue

        kind, status_word = _rss_classify(description)

        out.append(
            Incident(
                provider_key=provider["key"],
                provider_name=provider["name"],
                incident_id=guid.rsplit("/", 1)[-1] or guid,
                title=title,
                status=status_word,
                # These feeds carry no impact field. "minor" is the honest floor:
                # it never fabricates a major outage the provider did not declare.
                impact="none" if kind == "maintenance" else "minor",
                kind=kind,
                url=link or base,
                started_at=when,
                updated_at=when,
                components=_rss_components(description),
                latest_update=_truncate(description),
            )
        )
    return out


def _looks_resolved(text):
    lowered = text.lower()
    return "resolved" in lowered or "has been restored" in lowered


def _rss_components(description):
    match = re.search(r"affected components:\s*(.+?)(?:\n|$)", description, re.I)
    if not match:
        return []
    return [part.strip() for part in match.group(1).split(",") if part.strip()][:8]


def _parse_rfc822(value):
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value.strip())
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# gcp: one JSON document covering every Google Cloud product.
# --------------------------------------------------------------------------

def fetch_gcp(provider):
    wanted = [m.lower() for m in provider.get("match", [])]
    payload = _get_json(provider["base"].rstrip("/") + "/incidents.json")
    out = []
    for raw in payload:
        products = [p.get("title", "") for p in raw.get("affected_products", [])]
        haystack = " ".join(products + [raw.get("external_desc", "")]).lower()
        if wanted and not any(w in haystack for w in wanted):
            continue
        started = _parse_iso(raw.get("begin"))
        if not started:
            continue
        ended = _parse_iso(raw.get("end"))
        updates = raw.get("updates") or []
        latest = updates[-1] if updates else {}
        updated = _parse_iso(latest.get("modified") or latest.get("created")) or started
        severity = (raw.get("severity") or "").lower()
        out.append(
            Incident(
                provider_key=provider["key"],
                provider_name=provider["name"],
                incident_id=str(raw.get("id") or raw.get("number")),
                title=raw.get("external_desc") or "(untitled incident)",
                status="resolved" if ended else "investigating",
                impact="major" if severity == "high" else "minor",
                kind="incident",
                url="https://status.cloud.google.com" + (raw.get("uri") or ""),
                started_at=started,
                updated_at=ended or updated,
                components=products[:8],
                latest_update=_truncate(_strip_html(latest.get("text", ""))),
            )
        )
    return out


ADAPTERS = {
    "statuspage": fetch_statuspage,
    "rss": fetch_rss,
    "gcp": fetch_gcp,
}


def fetch(provider):
    adapter = ADAPTERS.get(provider.get("adapter"))
    if adapter is None:
        raise FetchError("unknown adapter %r for %s" % (provider.get("adapter"), provider["key"]))
    return adapter(provider)
