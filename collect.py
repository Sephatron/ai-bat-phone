#!/usr/bin/env python3
"""AI Bat Phone — poll AI provider status pages and publish an RSS feed.

Run with no arguments. Reads providers.toml, compares what each provider is
reporting against state.json, appends any genuine change to events.json, and
rewrites the feeds under docs/.

Design rule that matters more than any other here: a provider we failed to read
produces no events at all. Silence from a status page is not recovery, and
inventing a "resolved" item because a fetch timed out would make the feed worse
than useless during exactly the incident it exists for.
"""

import argparse
import json
import os
import re
import sys
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import adapters
import copywriter
import feedgen

ROOT = os.path.dirname(os.path.abspath(__file__))
PROVIDERS_FILE = os.path.join(ROOT, "providers.toml")
STATE_FILE = os.path.join(ROOT, "state.json")
EVENTS_FILE = os.path.join(ROOT, "events.json")
DOCS = os.path.join(ROOT, "docs")

SITE_URL = "https://sephatron.github.io/ai-bat-phone"

# An incident first seen with a start date older than this is backfilled
# silently. Without it, a provider extending their published history would dump
# months of ancient outages into the feed as breaking news.
MAX_NEW_AGE = timedelta(days=30)
# A never-before-seen incident that is nominally still open but has had no
# update in this long is not news, it is a status page nobody closed out.
# History feeds are full of these — an abandoned "[Scheduled]" entry from three
# weeks ago should not ring the bat phone.
STALE_OPEN = timedelta(days=7)
# How long a resolved incident stays in the seen-map before being forgotten.
STATE_TTL = timedelta(days=90)
EVENT_LOG_CAP = 400
FEED_CAP = 200
FETCH_WORKERS = 8


def load_providers():
    with open(PROVIDERS_FILE, "rb") as handle:
        data = tomllib.load(handle)
    return [p for p in data.get("provider", []) if p.get("enabled", True)]


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (FileNotFoundError, ValueError):
        return default


def write_json(path, payload):
    return write_text(path, json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


# Every generated file carries a build timestamp, which would otherwise make the
# working tree dirty on every single poll and produce a commit every ten minutes
# forever. Compare with those stamps masked out so quiet days write nothing.
VOLATILE = (
    re.compile(r"<lastBuildDate>.*?</lastBuildDate>"),
    re.compile(r"Updated \d{2} \w{3} \d{4} \d{2}:\d{2} UTC"),
)


def _stable(text):
    for pattern in VOLATILE:
        text = pattern.sub("", text)
    return text


def write_text(path, text):
    """Write only if the content differs beyond its build timestamp."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            if _stable(handle.read()) == _stable(text):
                return False
    except FileNotFoundError:
        pass
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return True


def gather(providers):
    """Fetch every provider concurrently. Returns (incidents, failures)."""
    incidents, failures = [], {}

    def one(provider):
        try:
            return provider, adapters.fetch(provider), None
        except adapters.FetchError as exc:
            return provider, [], str(exc)
        except Exception as exc:  # an adapter bug must not take the run down
            return provider, [], "unexpected %s: %s" % (type(exc).__name__, exc)

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for provider, found, error in pool.map(one, providers):
            if error:
                failures[provider["name"]] = error
                print("  ! %-22s %s" % (provider["key"], error), file=sys.stderr)
            else:
                incidents.extend(found)
                print("  · %-22s %d incidents" % (provider["key"], len(found)))
    return incidents, failures


def classify(incident, previous, bootstrap, now):
    """Return the transition name for this incident, or None to stay silent."""
    if previous is None:
        if incident.is_over:
            return None  # finished before we ever saw it — nothing to announce
        if now - incident.started_at > MAX_NEW_AGE:
            return None  # stale backlog entry, not news
        if now - incident.updated_at > STALE_OPEN:
            return None  # open on paper only; nobody has touched it in a week
        return "opened"

    if bootstrap:
        return None

    was_over = previous.get("status") in ("resolved", "postmortem", "completed")
    if incident.is_over and not was_over:
        return "resolved"
    if incident.is_over:
        return None

    old_rank = adapters.IMPACT_RANK.get(previous.get("impact", "none"), 0)
    new_rank = adapters.IMPACT_RANK.get(incident.impact, 0)
    if new_rank > old_rank:
        return "escalated"
    if incident.status != previous.get("status"):
        return "progress"
    return None


def make_event(incident, transition, now):
    duration = None
    if transition == "resolved":
        duration = incident.updated_at - incident.started_at
        if duration.total_seconds() < 0:
            duration = None

    published = incident.started_at if transition == "opened" else incident.updated_at
    # Some pages stamp updates slightly in the future; a future pubDate makes
    # readers hide the item entirely.
    published = min(published, now)

    return {
        "id": "%s:%s:%s:%s" % (incident.key, transition, incident.status, incident.impact),
        "provider_key": incident.provider_key,
        "provider_name": incident.provider_name,
        "incident_id": incident.incident_id,
        "transition": transition,
        "kind": incident.kind,
        "status": incident.status,
        "impact": incident.impact,
        "title": incident.title,
        "url": incident.url,
        "components": incident.components,
        "body": incident.latest_update,
        "duration": copywriter.format_duration(duration) if duration else "",
        "published_at": published.isoformat(),
        "headline": copywriter.headline(incident, transition, duration),
    }


def prune_state(seen, now):
    keep = {}
    for key, record in seen.items():
        last = record.get("updated_at")
        try:
            when = datetime.fromisoformat(last) if last else None
        except ValueError:
            when = None
        if when is None or now - when < STATE_TTL:
            keep[key] = record
    return keep


def main():
    parser = argparse.ArgumentParser(description="Poll AI status pages and rebuild the feed.")
    parser.add_argument("--dry-run", action="store_true", help="report changes without writing")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    providers = load_providers()
    state = load_json(STATE_FILE, {"version": 1, "seen": {}})
    seen = state.get("seen", {})
    events = load_json(EVENTS_FILE, [])
    known_ids = {e["id"] for e in events}
    bootstrap = not seen

    if bootstrap:
        print("First run: recording current state, announcing only live incidents.")

    print("Polling %d providers…" % len(providers))
    incidents, failures = gather(providers)

    # A total wipeout means the network, not the industry. Publishing nothing is
    # the correct outcome; publishing a rebuilt feed off an empty read is not.
    if not incidents and failures:
        print("Every provider failed. Leaving the feed untouched.", file=sys.stderr)
        return 1

    new_events = []
    for incident in incidents:
        previous = seen.get(incident.key)
        transition = classify(incident, previous, bootstrap, now)
        if transition:
            event = make_event(incident, transition, now)
            if event["id"] not in known_ids:
                new_events.append(event)
                known_ids.add(event["id"])
        seen[incident.key] = {
            "status": incident.status,
            "impact": incident.impact,
            "updated_at": incident.updated_at.isoformat(),
        }

    for event in new_events:
        print("  + %s" % event["headline"])
    print("%d new event(s), %d provider(s) unreachable." % (len(new_events), len(failures)))

    events = new_events + events
    events.sort(key=lambda e: e["published_at"], reverse=True)
    events = events[:EVENT_LOG_CAP]

    if args.dry_run:
        print("Dry run: nothing written.")
        return 0

    os.makedirs(DOCS, exist_ok=True)
    changed = write_json(STATE_FILE, {"version": 1, "seen": prune_state(seen, now)})
    changed |= write_json(EVENTS_FILE, events)
    for filename in feedgen.VARIANTS:
        changed |= write_text(
            os.path.join(DOCS, filename),
            feedgen.build_rss(events, filename, SITE_URL, max_items=FEED_CAP),
        )
    changed |= write_text(
        os.path.join(DOCS, "index.html"),
        feedgen.build_index(events, providers, failures, SITE_URL),
    )
    if not os.path.exists(os.path.join(DOCS, ".nojekyll")):
        write_text(os.path.join(DOCS, ".nojekyll"), "")
    print(("Wrote %s" % DOCS) if changed else "No content change; files left alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
