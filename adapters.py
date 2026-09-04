"""Fetch provider status pages and normalise them into one Incident shape.

Every adapter returns a list of Incident. Adapters raise FetchError on any
transport or parse problem; the caller treats that as "no information from this
provider this run" and must never interpret it as a resolution.

Everything here handles bytes from third parties nobody controls, so this module
is the trust boundary: strings are scrubbed of characters that cannot legally
appear in XML, and URLs are restricted to http/https, at the point incidents are
constructed rather than at each place they are later rendered.
"""

import gzip
import html
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

USER_AGENT = (
    "Mozilla/5.0 (compatible; ai-bat-phone/1.0; "
    "+https://github.com/Sephatron/ai-bat-phone)"
)
TIMEOUT = 20
# A status page has no legitimate reason to be large. Without a cap a hostile or
# broken host can hand us a gzip bomb: a 700KB response that decompresses to
# hundreds of megabytes and takes the runner down with it.
MAX_BYTES = 8 * 1024 * 1024
_CHUNK = 64 * 1024

# Ordered worst-last so a numeric comparison detects escalation.
IMPACT_RANK = {"none": 0, "maintenance": 0, "minor": 1, "major": 2, "critical": 3}

# The states that mean "this is finished". Shared with collect.py so the two
# modules cannot drift apart on the single question the diff engine turns on.
OVER_STATUSES = ("resolved", "postmortem", "completed")
INCIDENT_STATUSES = ("investigating", "identified", "monitoring", "resolved", "postmortem")
MAINTENANCE_STATUSES = ("scheduled", "in_progress", "verifying", "completed")

# Characters that are illegal in XML 1.0 even escaped. One of these in a title
# makes the whole feed unparseable for every subscriber, and because the title
# is persisted to events.json it would stay broken long after the provider fixed
# their page. Strip them on the way in.
_ILLEGAL_XML = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\uD800-\uDFFF￾￿]")

MAX_TITLE = 300
MAX_URL = 500
MAX_ID = 200


class FetchError(Exception):
    """A provider could not be read this run. Never a signal about their uptime."""


def xml_safe(value, limit=None):
    """Make a provider-supplied string safe to put in a feed, and bounded."""
    if not value:
        return ""
    cleaned = _ILLEGAL_XML.sub("", str(value)).strip()
    if limit and len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip() + "…"
    return cleaned


# quoteattr() would render these safely on its own, but a URL containing a raw
# quote or angle bracket is malformed anyway, and refusing it here means the
# whole class of attribute-breakout bugs cannot reach any future sink either.
_URL_FORBIDDEN = re.compile(r"""['"<>\s\\]""")


def safe_url(value, fallback):
    """Only well-formed http(s) URLs reach the page.

    A javascript: or data: link in an href executes on our own origin, which is
    shared with every other GitHub Pages project on this account.
    """
    candidate = xml_safe(value, MAX_URL)
    if not candidate or _URL_FORBIDDEN.search(candidate):
        return fallback
    try:
        scheme = urllib.parse.urlparse(candidate).scheme.lower()
    except ValueError:
        return fallback
    return candidate if scheme in ("http", "https") else fallback


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
    # False for history-feed sources, which stamp every entry with the incident's
    # start time and mutate the body in place. For those, updated_at is a lie and
    # no duration can honestly be derived from it.
    updates_tracked: bool = True

    def __post_init__(self):
        # Every string, not just the obviously provider-supplied ones. The
        # docstring at the top of this module calls this the trust boundary; it
        # is only true if nothing gets to skip it.
        self.provider_key = xml_safe(self.provider_key, 60)
        self.provider_name = xml_safe(self.provider_name, 80)
        self.status = xml_safe(self.status, 40)
        self.impact = xml_safe(self.impact, 20)
        self.kind = xml_safe(self.kind, 20)
        self.incident_id = xml_safe(self.incident_id, MAX_ID) or "unknown"
        self.title = xml_safe(self.title, MAX_TITLE) or "(untitled incident)"
        self.latest_update = xml_safe(self.latest_update, 600)
        self.components = [xml_safe(c, 120) for c in self.components if xml_safe(c, 120)][:8]
        self.url = safe_url(self.url, "https://github.com/Sephatron/ai-bat-phone")

    @property
    def key(self):
        return "%s:%s" % (self.provider_key, self.incident_id)

    @property
    def is_over(self):
        return self.status in OVER_STATUSES


def _read_capped(stream, gzipped):
    """Read at most MAX_BYTES, counting decompressed bytes, not wire bytes."""
    source = gzip.GzipFile(fileobj=stream) if gzipped else stream
    chunks, total = [], 0
    while True:
        chunk = source.read(_CHUNK)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_BYTES:
            raise FetchError("response exceeded %d bytes" % MAX_BYTES)
        chunks.append(chunk)
    return b"".join(chunks)


def _get(url, accept):
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": accept, "Accept-Encoding": "gzip"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            if not resp.url.lower().startswith("https://"):
                # urllib follows redirects silently. An https page bounced to
                # plain http hands a network attacker a write path into the feed.
                raise FetchError(
                    "%s ended on a non-https URL (%s)" % (url, resp.url)
                    if resp.url != url
                    else "%s is not https" % url
                )
            gzipped = resp.headers.get("Content-Encoding") == "gzip"
            return resp.status, _read_capped(io.BytesIO(resp.read(MAX_BYTES + 1)), gzipped)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except FetchError:
        raise
    except Exception as exc:  # socket errors, DNS, TLS, redirect loops
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
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _strip_html(value):
    """Unescape first, then strip.

    The other order turns "&lt;img onerror=…&gt;" into a live tag: unescaping
    after the tag regex has already run means nothing removes it.
    """
    if not value:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</p>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


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
    if not isinstance(payload, dict) or "incidents" not in payload:
        raise FetchError("%s: no 'incidents' key — response shape changed" % base)
    for raw in payload.get("incidents") or []:
        incident = _statuspage_incident(provider, raw, base)
        if incident:
            out.append(incident)

    # Only Atlassian serves this one; Instatus and incident.io answer 404, so a
    # missing maintenance endpoint is normal and must not fail the provider.
    try:
        maint = _get_json(base + "/api/v2/scheduled-maintenances.json")
    except FetchError:
        maint = None
    if isinstance(maint, dict):
        for raw in maint.get("scheduled_maintenances") or []:
            incident = _statuspage_incident(provider, raw, base, kind="maintenance")
            if incident:
                out.append(incident)
    return out


def _statuspage_incident(provider, raw, base, kind="incident"):
    if not isinstance(raw, dict):
        return None
    incident_id = raw.get("id")
    if not incident_id:
        return None
    started = _parse_iso(raw.get("started_at") or raw.get("created_at") or raw.get("scheduled_for"))
    updated = _parse_iso(raw.get("updated_at")) or started
    if not started:
        return None
    updates = raw.get("incident_updates") or []
    body = _strip_html(updates[0].get("body", "")) if updates else ""
    status = str(raw.get("status") or "").lower()
    allowed = MAINTENANCE_STATUSES if kind == "maintenance" else INCIDENT_STATUSES
    if status not in allowed:
        # Coercing an unknown word to a live status ("investigating") turns
        # every already-resolved incident into a reopening the moment a status
        # host adds a vocabulary word. Skip it, the same way _rss_classify
        # refuses to guess at a dialect it does not know.
        print(
            "  ? %-22s unknown %s status %r, skipping incident %s"
            % (provider.get("key"), kind, status, incident_id),
            file=sys.stderr,
        )
        return None
    impact = str(raw.get("impact") or "none").lower()
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
        components=[c.get("name") for c in raw.get("components", []) if isinstance(c, dict) and c.get("name")],
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
_RSS_STATUS_LINE = re.compile(r"\bstatus:\s*([a-z]+)", re.I)
_RSS_TYPE_LINE = re.compile(r"\btype:\s*(incident|maintenance)\b", re.I)
_RSS_DURATION = re.compile(r"\bduration:\s*\d", re.I)
# "not yet resolved", "has not been resolved" — a bare substring test on prose
# reads these as an all-clear and silently drops a live outage.
_NEGATED_RESOLVED = re.compile(r"\b(not|isn't|hasn't|haven't|yet to be)\b[^.]{0,40}\bresolved\b", re.I)

_MARKER_TO_STATUS = {
    "resolved": ("incident", "resolved"),
    "mitigated": ("incident", "monitoring"),
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
    """Work out (kind, status) for one history-feed entry, or None if unsure.

    Returning None matters more than it looks. The alternative — guessing
    "investigating" for anything unrecognised — means a dialect this parser has
    never seen becomes a stream of outages that never resolve, indistinguishable
    from the real thing. An unknown entry is skipped and counted instead, so a
    provider changing their format shows up as a failure rather than as noise.
    """
    marker = _RSS_MARKER.match(text)
    if marker:
        word = marker.group(1).strip().lower().replace(" ", "_")
        if word in _MARKER_TO_STATUS:
            return _MARKER_TO_STATUS[word]
        return None  # a bracket marker we do not know is a dialect change

    line = _RSS_STATUS_LINE.search(text)
    if line:
        word = line.group(1).strip().lower()
        if word in _MARKER_TO_STATUS:
            return _MARKER_TO_STATUS[word]
        return None

    type_line = _RSS_TYPE_LINE.search(text)
    kind = "maintenance" if type_line and type_line.group(1).lower() == "maintenance" else "incident"
    if _RSS_DURATION.search(text):
        # A published duration means the provider has closed it out.
        return kind, ("completed" if kind == "maintenance" else "resolved")

    if type_line:
        # This dialect states a duration when, and only when, it is finished.
        # Falling through to the keyword sniff below would read the standard
        # "service has been restored and we are monitoring" line as an
        # all-clear while the incident is still open.
        return kind, ("scheduled" if kind == "maintenance" else "investigating")
    if _NEGATED_RESOLVED.search(text):
        return kind, "investigating"
    if _looks_resolved(text):
        return kind, "resolved"
    return None


def fetch_rss(provider):
    import xml.etree.ElementTree as ET

    base = provider["base"].rstrip("/")
    path = provider.get("path", "/history.rss")
    url = base + path
    # ET uses expat, which since 2.4 refuses entity expansion and caps input
    # amplification, so billion-laughs and XXE are blocked by the parser itself.
    # There is no defusedxml here; that protection is the dependency.
    status, raw = _get(url, "application/rss+xml, application/xml")
    if status != 200:
        raise FetchError("%s returned HTTP %s" % (url, status))
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise FetchError("%s is not valid XML: %s" % (url, exc))

    out, seen_items, unknown = [], 0, 0
    for item in root.iterfind(".//channel/item"):
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        if not guid:
            continue
        when = _parse_rfc822(item.findtext("pubDate"))
        if not when:
            continue
        seen_items += 1

        description = _strip_html(item.findtext("description") or "")
        classified = _rss_classify(description)
        if classified is None:
            unknown += 1
            continue
        kind, status_word = classified

        out.append(
            Incident(
                provider_key=provider["key"],
                provider_name=provider["name"],
                incident_id=guid.rsplit("/", 1)[-1] or guid,
                title=(item.findtext("title") or "(untitled incident)").strip(),
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
                updates_tracked=False,
            )
        )

    if not seen_items:
        # The counter sits after the guid and pubDate guards, so a date format
        # change, a move to Atom, or a different channel path yields zero here.
        # Returning [] would look like a calm provider and reset the failure
        # counter, which is the one outcome this project must never produce.
        raise FetchError("%s: parsed no usable entries — feed shape changed" % url)
    if unknown >= seen_items / 2:
        raise FetchError("%s: %d of %d entries in an unrecognised format" % (url, unknown, seen_items))
    if unknown:
        print("  ? %-22s %d entry(s) in an unrecognised format" % (provider["key"], unknown), file=sys.stderr)
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
    if not isinstance(payload, list):
        raise FetchError("%s: incidents.json is not a list — shape changed" % provider["base"])
    out = []
    for raw in payload:
        if not isinstance(raw, dict):
            continue
        products = [p.get("title", "") for p in raw.get("affected_products", []) if isinstance(p, dict)]
        haystack = " ".join(products + [str(raw.get("external_desc", ""))]).lower()
        if wanted and not any(w in haystack for w in wanted):
            continue
        started = _parse_iso(raw.get("begin"))
        if not started:
            continue
        ended = _parse_iso(raw.get("end"))
        updates = raw.get("updates") or []
        latest = updates[-1] if updates and isinstance(updates[-1], dict) else {}
        updated = _parse_iso(latest.get("modified") or latest.get("created")) or started
        severity = str(raw.get("severity") or "").lower()
        out.append(
            Incident(
                provider_key=provider["key"],
                provider_name=provider["name"],
                incident_id=str(raw.get("id") or raw.get("number")),
                title=raw.get("external_desc") or "(untitled incident)",
                status="resolved" if ended else "investigating",
                impact="major" if severity == "high" else "minor",
                kind="incident",
                url="https://status.cloud.google.com" + str(raw.get("uri") or ""),
                started_at=started,
                updated_at=ended or updated,
                components=products[:8],
                latest_update=_truncate(_strip_html(str(latest.get("text", "")))),
            )
        )
    return out


ADAPTERS = {
    "statuspage": fetch_statuspage,
    "rss": fetch_rss,
    "gcp": fetch_gcp,
}

# Which adapters can ever report scheduled maintenance. Surfaced on the index so
# a subscriber to outages.xml knows which providers the filter is a no-op for.
MAINTENANCE_CAPABLE = ("statuspage", "rss")


def fetch(provider):
    adapter = ADAPTERS.get(provider.get("adapter"))
    if adapter is None:
        raise FetchError("unknown adapter %r for %s" % (provider.get("adapter"), provider.get("key")))
    return adapter(provider)
