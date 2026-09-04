#!/usr/bin/env python3
"""AI Bat Phone — poll AI provider status pages and publish an RSS feed.

Run with no arguments. Reads providers.toml, compares what each provider is
reporting against state.json, appends any genuine change to events.json, and
rewrites the feeds under docs/.

Three rules shape everything here:

1. A provider we failed to read produces no incident events. Silence from a
   status page is not recovery, and inventing a "resolved" because a fetch timed
   out would make the feed worse than useless during the incident it exists for.

2. Instead, a provider we cannot read for several runs gets said out loud, in
   the feed. A monitoring feed whose own death looks like good news is the worst
   failure mode available to it.

3. Items are stamped with the time we noticed, not the time the provider says
   the incident began. This is an alert stream, not an archive: an all-clear
   backdated three days sorts below everything already read and is never seen.
"""

import argparse
import json
import os
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

STATE_VERSION = 2

# An incident first seen with a start date older than this is backfilled
# silently. Without it, a provider extending their published history would dump
# months of ancient outages into the feed as breaking news.
MAX_NEW_AGE = timedelta(days=30)
# A never-before-seen incident that is nominally still open but has had no
# update in this long is not news, it is a status page nobody closed out.
# History feeds are full of these.
STALE_OPEN = timedelta(days=7)
# How long after we last saw an incident in a fetch we keep it in the seen-map.
# Keyed on our own sighting, never on the provider's timestamp: a long-running
# outage that nobody updates for months is still live, and forgetting it would
# throw away the all-clear.
STATE_TTL = timedelta(days=90)
# Consecutive failed polls before we say so in the feed. Three is about half an
# hour at the normal cadence — long enough to ride out a blip, short enough to
# tell you the monitor is blind while it still matters.
FAILURES_BEFORE_ALARM = 3

EVENT_LOG_CAP = 400
FEED_CAP = 200
FETCH_WORKERS = 8


class CorruptState(Exception):
    """State exists but cannot be read. Never treat this as a first run."""


def load_providers():
    """Return (enabled providers, documented gaps).

    The gaps are the blocks marked `enabled = false` — providers with no
    reachable endpoint. They are published on the index too: a reader who sees
    no Mistral outages should know Mistral is not being watched, rather than
    concluding Mistral has been having a good month.
    """
    with open(PROVIDERS_FILE, "rb") as handle:
        data = tomllib.load(handle)
    providers, gaps = [], []
    for index, provider in enumerate(data.get("provider", [])):
        missing = [f for f in ("key", "name", "adapter", "base") if not provider.get(f)]
        if missing:
            raise SystemExit(
                "providers.toml: block %d is missing %s" % (index + 1, ", ".join(missing))
            )
        if provider.get("enabled", True):
            providers.append(provider)
        else:
            gaps.append(provider)
    keys = [p["key"] for p in providers]
    duplicates = {k for k in keys if keys.count(k) > 1}
    if duplicates:
        raise SystemExit("providers.toml: duplicate key(s) %s" % ", ".join(sorted(duplicates)))
    return providers, gaps


def load_json(path, default):
    """Missing means first run. Present but unreadable means stop, loudly."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except ValueError as exc:
        raise CorruptState("%s exists but is not valid JSON: %s" % (path, exc))


def write_text(path, text):
    """Write atomically, and only if the content actually differs."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            if handle.read() == text:
                return False
    except FileNotFoundError:
        pass
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.replace(temp, path)
    return True


def write_json(path, payload):
    return write_text(path, json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=False) + "\n")


def heartbeat_of(now):
    """The poll clock, rounded to the hour.

    Every generated file carries this, which is what lets a subscriber tell a
    quiet week from a dead collector. Rounding to the hour is the whole trick:
    at full resolution it would produce a commit every ten minutes forever, and
    at no resolution the feed cannot say when it last looked.
    """
    return now.replace(minute=0, second=0, microsecond=0)


def gather(providers):
    """Fetch every provider concurrently. Returns (incidents, errors_by_key)."""
    incidents, errors = [], {}

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
                errors[provider["key"]] = error
                print("  ! %-22s %s" % (provider["key"], error), file=sys.stderr)
            else:
                incidents.extend(found)
                print("  · %-22s %d incidents" % (provider["key"], len(found)))
    return incidents, errors


def classify(incident, previous, now):
    """Return the transition name for this incident, or None to stay silent."""
    if previous is None:
        if incident.is_over:
            return None  # finished before we ever saw it — nothing to announce
        if now - incident.started_at > MAX_NEW_AGE:
            return None  # stale backlog entry, not news
        if now - incident.updated_at > STALE_OPEN:
            return None  # open on paper only; nobody has touched it in a week
        return "opened"

    was_over = previous.get("status") in adapters.OVER_STATUSES
    if incident.is_over:
        return None if was_over else "resolved"
    if was_over:
        # Providers do reopen incidents when a fix regresses. Without its own
        # transition this would read as a mid-incident update, and its eventual
        # resolution would collide with the first one and never be published.
        return "reopened"

    old_rank = adapters.IMPACT_RANK.get(previous.get("impact", "none"), 0)
    new_rank = adapters.IMPACT_RANK.get(incident.impact, 0)
    if new_rank > old_rank:
        return "escalated"
    if incident.status != previous.get("status"):
        return "progress"
    return None


def make_event(incident, transition, now):
    duration = None
    if transition == "resolved" and incident.updates_tracked:
        span = incident.updated_at - incident.started_at
        duration = span if span.total_seconds() > 0 else None

    return {
        # The provider's update time is part of the id so a flapping incident
        # (identified -> monitoring -> identified) reports every move, and a
        # reopened one can be resolved a second time.
        "id": "%s:%s:%s:%s:%s"
        % (incident.key, transition, incident.status, incident.impact, incident.updated_at.isoformat()),
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
        "started_at": incident.started_at.isoformat(),
        "published_at": now.isoformat(),
        "headline": copywriter.headline(incident, transition, duration),
    }


def make_meta_event(provider, transition, since, detail, now):
    """An event about the monitor itself rather than about a provider's uptime."""
    return {
        "id": "meta:%s:%s:%s" % (provider["key"], transition, since),
        "provider_key": provider["key"],
        "provider_name": provider["name"],
        "incident_id": "monitor",
        "transition": transition,
        "kind": "meta",
        "status": transition,
        "impact": "none",
        "title": "AI Bat Phone cannot read %s's status page" % provider["name"]
        if transition == "unreachable"
        else "AI Bat Phone can read %s's status page again" % provider["name"],
        "url": provider["base"],
        "components": [],
        "body": detail,
        "duration": "",
        "started_at": since,
        "published_at": now.isoformat(),
        "headline": copywriter.meta_headline(provider["name"], transition),
    }


def track_providers(providers, errors, tracked, now, beat=None):
    """Update per-provider health and return any meta events to publish.

    Bookkeeping timestamps are stored at the heartbeat's resolution, not the
    poll's. At full resolution every quiet run would rewrite state.json and the
    Action would commit every ten minutes forever.
    """
    beat = beat or heartbeat_of(now)
    events = []
    for provider in providers:
        key = provider["key"]
        record = tracked.setdefault(key, {"consecutive_failures": 0})
        error = errors.get(key)
        if error:
            record["consecutive_failures"] = record.get("consecutive_failures", 0) + 1
            record["last_error"] = error
            record.setdefault("failing_since", now.isoformat())
            if record["consecutive_failures"] == FAILURES_BEFORE_ALARM:
                events.append(
                    make_meta_event(provider, "unreachable", record["failing_since"], error, now)
                )
        else:
            if record.get("consecutive_failures", 0) >= FAILURES_BEFORE_ALARM:
                events.append(
                    make_meta_event(
                        provider, "recovered", record["failing_since"], "", now
                    )
                )
            record["consecutive_failures"] = 0
            record["last_success"] = beat.isoformat()
            record.pop("failing_since", None)
            record.pop("last_error", None)
    return events


def unreachable_now(providers, tracked):
    """Providers that have failed often enough to be worth showing on the page.

    A single flaky poll is not reported. Rendering every transient timeout would
    rewrite index.html and produce a commit, then another when it cleared.
    """
    out = {}
    for provider in providers:
        record = tracked.get(provider["key"], {})
        if record.get("consecutive_failures", 0) >= FAILURES_BEFORE_ALARM:
            out[provider["name"]] = record.get("last_error", "unknown error")
    return out


def prune_state(seen, now):
    keep = {}
    for key, record in seen.items():
        last = record.get("last_seen_at") or record.get("updated_at")
        try:
            when = datetime.fromisoformat(last) if last else None
        except (ValueError, TypeError):
            when = None
        if when is None or now - when < STATE_TTL:
            keep[key] = record
    return keep


def main():
    parser = argparse.ArgumentParser(description="Poll AI status pages and rebuild the feed.")
    parser.add_argument("--dry-run", action="store_true", help="report changes without writing")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    beat = heartbeat_of(now)
    providers, gaps = load_providers()
    try:
        state = load_json(STATE_FILE, {"version": STATE_VERSION, "seen": {}, "providers": {}})
        events = load_json(EVENTS_FILE, [])
    except CorruptState as exc:
        # Falling back to defaults here would look exactly like a first run and
        # would silently re-announce every open incident across every provider.
        print("Refusing to run: %s" % exc, file=sys.stderr)
        print("Fix or delete the file deliberately, then re-run.", file=sys.stderr)
        return 1

    seen = state.get("seen", {})
    tracked = state.get("providers", {})
    known_ids = {e["id"] for e in events}

    if not seen:
        print("First run: recording current state, announcing only live incidents.")

    print("Polling %d providers…" % len(providers))
    incidents, errors = gather(providers)

    if len(errors) == len(providers):
        # Everything failing at once means our network, not the industry.
        print("Every provider failed. Leaving the feed untouched.", file=sys.stderr)
        return 1

    new_events = track_providers(providers, errors, tracked, now, beat)

    for incident in incidents:
        previous = seen.get(incident.key)
        transition = classify(incident, previous, now)
        if transition:
            event = make_event(incident, transition, now)
            if event["id"] not in known_ids:
                new_events.append(event)
                known_ids.add(event["id"])
        seen[incident.key] = {
            "status": incident.status,
            "impact": incident.impact,
            "updated_at": incident.updated_at.isoformat(),
            # Hour-quantised on purpose: see track_providers.
            "last_seen_at": beat.isoformat(),
        }

    for event in new_events:
        print("  + %s" % event["headline"])
    print(
        "%d new event(s), %d provider(s) unreadable this run."
        % (len(new_events), len(errors))
    )

    events = new_events + events
    events.sort(key=lambda e: e["published_at"], reverse=True)
    events = events[:EVENT_LOG_CAP]

    if args.dry_run:
        print("Dry run: nothing written.")
        return 0

    os.makedirs(DOCS, exist_ok=True)
    changed = write_json(
        STATE_FILE,
        {
            "version": STATE_VERSION,
            "heartbeat": beat.isoformat(),
            "seen": prune_state(seen, now),
            "providers": tracked,
        },
    )
    changed |= write_json(EVENTS_FILE, events)
    for filename in feedgen.VARIANTS:
        changed |= write_text(
            os.path.join(DOCS, filename), feedgen.build_rss(events, filename, beat, max_items=FEED_CAP)
        )
    changed |= write_text(
        os.path.join(DOCS, "index.html"),
        feedgen.build_index(events, providers, unreachable_now(providers, tracked), beat, gaps),
    )
    if not os.path.exists(os.path.join(DOCS, ".nojekyll")):
        write_text(os.path.join(DOCS, ".nojekyll"), "")
    print(("Wrote %s" % DOCS) if changed else "No content change; files left alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
