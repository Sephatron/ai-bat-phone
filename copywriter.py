"""Headline copy for feed items.

The joke lives in the title. The body stays factual, because someone reading
this feed at 09:00 on a Tuesday is trying to work out whether it's them or the
provider, and a punchline is no use to them.

Selection is deterministic — the same incident always gets the same line — so a
re-run or a rebuild of the feed never reshuffles headlines under subscribers.
"""

import hashlib
import re

# Anything matching these gets neutral copy. Jokes about a breach age badly, and
# a security notice is the one case where a reader must not have to decode tone.
SOBER = re.compile(
    r"\b(security|breach|vulnerabilit|CVE-|exploit|unauthori[sz]ed|data loss|"
    r"exposed|leak|compromis|incident report|phishing|malicious)\b",
    re.I,
)

OPENED_HIGH = [
    "{p} has downed tools — and so, probably, have you",
    "{p} is on the floor. Kettle on, grass outside.",
    "{p} is having a proper one. This is not your code.",
    "Tools down: {p} is broken",
    "{p} has gone dark. Nothing you did. Probably.",
    "{p} is out. Go and look at a tree.",
]

OPENED_LOW = [
    "{p} is having a wobble",
    "{p} is a bit under the weather",
    "Minor grumbles from {p}",
    "{p} is sulking, mildly",
    "{p} has a limp, not a break",
]

ESCALATED = [
    "It got worse: {p} is now a {impact} outage",
    "{p} has escalated. Down tools properly now.",
    "Upgrade your concern: {p} is now {impact}",
    "{p} went from annoying to actual",
]

IDENTIFIED = [
    "{p} has found the culprit",
    "{p} knows what it did",
    "{p} has identified the problem. Fix pending.",
]

MONITORING = [
    "{p} thinks it's fixed and is watching nervously",
    "{p} is on the mend. Don't get comfortable yet.",
    "{p} has applied a fix and is holding its breath",
]

RESOLVED_HIGH = [
    "{p} is back. Grass sufficiently touched.",
    "{p} lives. Pick your tools back up.",
    "All clear from {p} after {duration}",
    "{p} is fixed. You may resume being productive.",
    "{p} is upright again. That took {duration}.",
]

RESOLVED_LOW = [
    "{p} has stopped wobbling",
    "{p} is fine again",
    "{p} sorted itself out after {duration}",
]

MAINTENANCE = {
    "scheduled": [
        "{p} is booking time off: {title}",
        "Planned works at {p}",
        "{p} has scheduled a lie-down",
    ],
    "in_progress": [
        "{p} is under the bonnet right now",
        "Maintenance underway at {p}",
    ],
    "verifying": [
        "{p} is checking its own homework",
    ],
    "completed": [
        "{p} has put the tools away",
        "Maintenance done at {p}",
    ],
}

EMOJI = {
    "opened_high": "🔴",
    "opened_low": "🟠",
    "escalated": "🔺",
    "identified": "🔍",
    "monitoring": "👀",
    "resolved": "🟢",
    "maintenance": "🔧",
    "sober": "⚠️",
}


def _pick(pool, seed):
    index = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16) % len(pool)
    return pool[index]


def format_duration(delta):
    if delta is None:
        return "a while"
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return "%d min" % minutes
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return "%dh %02dm" % (hours, minutes)
    days, hours = divmod(hours, 24)
    return "%dd %dh" % (days, hours)


def headline(incident, transition, duration=None):
    """Return the feed item title for one incident transition."""
    fields = {
        "p": incident.provider_name,
        "title": incident.title,
        "impact": incident.impact,
        "duration": format_duration(duration),
    }
    seed = "%s|%s" % (incident.key, transition)

    if SOBER.search("%s %s" % (incident.title, incident.latest_update)):
        return "%s %s: %s" % (EMOJI["sober"], incident.provider_name, incident.title)

    if incident.kind == "maintenance":
        pool = MAINTENANCE.get(incident.status) or MAINTENANCE["scheduled"]
        return "%s %s" % (EMOJI["maintenance"], _pick(pool, seed).format(**fields))

    high = incident.impact in ("major", "critical")

    # Status wins over transition for the middle of an incident's life: an
    # incident we meet for the first time already at "monitoring" should not be
    # announced as if it just broke.
    if transition == "escalated":
        pool, badge = ESCALATED, EMOJI["escalated"]
    elif transition == "resolved":
        pool = RESOLVED_HIGH if high else RESOLVED_LOW
        badge = EMOJI["resolved"]
    elif incident.status == "identified":
        pool, badge = IDENTIFIED, EMOJI["identified"]
    elif incident.status == "monitoring":
        pool, badge = MONITORING, EMOJI["monitoring"]
    elif transition == "opened":
        pool = OPENED_HIGH if high else OPENED_LOW
        badge = EMOJI["opened_high"] if high else EMOJI["opened_low"]
    else:
        # Any lifecycle state without its own voice falls back to the facts.
        return "%s %s: %s" % (EMOJI["opened_low"], incident.provider_name, incident.title)

    return "%s %s" % (badge, _pick(pool, seed).format(**fields))
