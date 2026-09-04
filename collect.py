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
from concurrent.futures import ThreadPoolExecutor, wait
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
# How long the whole fetch phase gets. TIMEOUT in adapters.py is a per-read
# socket timeout, which a server dripping one byte every few seconds resets
# forever; without a deadline here one such host hangs the run until the job is
# killed, and then nothing is written and nothing is announced for anybody.
FETCH_DEADLINE = 90
# Proof of life for subscribers. lastBuildDate is not shown by any mainstream
# reader, so on its own it tells a person nothing. This item does.
ALIVE_EVERY = timedelta(days=7)

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
    try:
        with open(PROVIDERS_FILE, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SystemExit("providers.toml could not be read: %s" % exc)
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
    keys = [p["key"] for p in providers + gaps]
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
    except OSError as exc:
        raise CorruptState("%s could not be read: %s" % (path, exc))


def validate_state(state, path):
    """Check the shape, not just that it parsed.

    Valid JSON of the wrong shape was the dangerous case: a state file missing
    its `seen` key looked exactly like a first run, and a first run re-announces
    every open incident across every provider at once.
    """
    if not isinstance(state, dict):
        raise CorruptState("%s is %s, expected an object" % (path, type(state).__name__))
    version = state.get("version")
    if version is not None and version > STATE_VERSION:
        raise CorruptState("%s is version %s; this code understands %s" % (path, version, STATE_VERSION))
    for field in ("seen", "providers"):
        value = state.get(field)
        if value is None:
            if version is not None:
                raise CorruptState("%s has no %r key" % (path, field))
            continue  # a genuinely absent file has already returned the default
        if not isinstance(value, dict):
            raise CorruptState("%s: %r is %s, expected an object" % (path, field, type(value).__name__))
        for key, record in value.items():
            if not isinstance(record, dict):
                raise CorruptState("%s: %s[%r] is not an object" % (path, field, key))
    return state


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


def day_of(now):
    """Day resolution for the seen-map.

    `last_seen_at` exists only to feed a 90-day TTL, so hour precision was 2,160
    times finer than the decision needs — and it rewrote 713 of state.json's
    4,242 lines every hour to record one fact the feeds already carry.
    """
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


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

    pool = ThreadPoolExecutor(max_workers=FETCH_WORKERS)
    futures = {pool.submit(one, p): p for p in providers}
    done, pending = wait(futures, timeout=FETCH_DEADLINE)
    for future in done:
        provider, found, error = future.result()
        if error:
            errors[provider["key"]] = error
            print("  ! %-22s %s" % (provider["key"], error), file=sys.stderr)
        else:
            incidents.extend(found)
            print("  · %-22s %d incidents" % (provider["key"], len(found)))
    for future in pending:
        provider = futures[future]
        errors[provider["key"]] = "no response within %ds" % FETCH_DEADLINE
        print("  ! %-22s %s" % (provider["key"], errors[provider["key"]]), file=sys.stderr)
    # Not a context manager, and not waiting: a thread stuck on a drip-feeding
    # socket would otherwise hold the run open past the job's own deadline.
    pool.shutdown(wait=False, cancel_futures=True)
    return incidents, errors, bool(pending)


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

    # A moving component in the id is what lets a flapping incident report every
    # move and a reopened one resolve a second time. For history feeds the
    # provider's update time never moves — every entry carries the incident's
    # start — so using it there would silently restore the collision it was
    # added to prevent. Fall back to when we noticed.
    moment = incident.updated_at if incident.updates_tracked else now
    return {
        "id": "%s:%s:%s:%s:%s"
        % (incident.key, transition, incident.status, incident.impact, moment.isoformat()),
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
    """An event about the monitor itself rather than about a provider's uptime.

    `detail` is an error string built from a remote server's own bytes, so it
    goes through the same scrubber as anything an adapter produces. This is the
    one path that reaches a feed without constructing an Incident, and skipping
    the boundary here was enough to make all three feeds unparseable.
    """
    detail = adapters.xml_safe(detail, 300)
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
        "url": adapters.safe_url(provider["base"], feedgen.SOURCE_URL),
        "components": [],
        "body": detail,
        "duration": "",
        "started_at": since,
        "published_at": now.isoformat(),
        "headline": copywriter.meta_headline(provider["name"], transition),
    }


def alive_event(events, providers, now):
    """A periodic "still watching" item, or None if one is not due yet.

    No mainstream reader shows lastBuildDate to a human, so on its own the
    heartbeat proves liveness to nobody who is not running curl. This does.
    """
    for event in events:
        if event.get("transition") == "alive":
            try:
                last = datetime.fromisoformat(event["published_at"])
            except (KeyError, ValueError):
                continue
            if now - last < ALIVE_EVERY:
                return None
            break
    return {
        "id": "meta:all:alive:%s" % now.date().isoformat(),
        "provider_key": "batphone",
        "provider_name": "AI Bat Phone",
        "incident_id": "monitor",
        "transition": "alive",
        "kind": "meta",
        "status": "alive",
        "impact": "none",
        "title": "Still watching %d provider status pages" % len(providers),
        "url": feedgen.SITE_URL,
        "components": [],
        "body": "Nothing to report. This item exists so that a feed which has "
        "gone quiet because the collector died looks different from one that is "
        "quiet because the models are behaving.",
        "duration": "",
        "started_at": now.isoformat(),
        "published_at": now.isoformat(),
        "headline": copywriter.meta_headline("AI Bat Phone", "alive"),
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
            previous = record.get("consecutive_failures", 0)
            # Capped: an uncapped counter rewrites state.json on every poll for
            # as long as one provider is down, which turns 24 commits a day into
            # 144 for as long as the outage lasts.
            record["consecutive_failures"] = min(previous + 1, FAILURES_BEFORE_ALARM)
            record["last_error"] = error
            record.setdefault("failing_since", beat.isoformat())
            if previous + 1 == FAILURES_BEFORE_ALARM:
                events.append(
                    make_meta_event(provider, "unreachable", record["failing_since"], error, now)
                )
        else:
            if record.get("consecutive_failures", 0) >= FAILURES_BEFORE_ALARM:
                events.append(
                    make_meta_event(
                        provider, "recovered", record.get("failing_since", beat.isoformat()), "", now
                    )
                )
            record["consecutive_failures"] = 0
            record.pop("failing_since", None)
            record.pop("last_error", None)
    # A provider deleted from providers.toml while failing would otherwise keep
    # its record for ever and never get its recovery item.
    for stale in set(tracked) - {p["key"] for p in providers}:
        del tracked[stale]
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="Poll AI status pages and rebuild the feed.")
    parser.add_argument("--dry-run", action="store_true", help="report changes without writing")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    beat = heartbeat_of(now)
    providers, gaps = load_providers()
    try:
        state = validate_state(
            load_json(STATE_FILE, {"version": STATE_VERSION, "seen": {}, "providers": {}}), STATE_FILE
        )
        events = load_json(EVENTS_FILE, [])
        if not isinstance(events, list):
            raise CorruptState("%s is not a list" % EVENTS_FILE)
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
    incidents, errors, abandoned = gather(providers)

    if not providers:
        print("No providers are enabled in providers.toml.", file=sys.stderr)
        return 1
    if len(errors) == len(providers):
        # Everything failing at once means our network, not the industry.
        print("Every provider failed. Leaving the feed untouched.", file=sys.stderr)
        return 1

    new_events = track_providers(providers, errors, tracked, now, beat)
    due = alive_event(events, providers, now)
    if due:
        new_events.append(due)

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
            # Day-quantised on purpose: see day_of.
            "last_seen_at": day_of(now).isoformat(),
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
    # events.json first, deliberately. A crash between the two writes then leaves
    # an event recorded but not marked seen, so the next run recomputes the same
    # transition, produces the same id, and known_ids swallows it. The other
    # order loses the event for ever.
    changed = write_json(EVENTS_FILE, events)
    changed |= write_json(
        STATE_FILE,
        {
            "version": STATE_VERSION,
            "heartbeat": beat.isoformat(),
            "seen": prune_state(seen, now),
            "providers": tracked,
        },
    )
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
    # docs/feed.xsl is committed by hand, not generated. Guard against it going
    # missing, which would leave every feed URL pointing at a stylesheet 404.
    if not os.path.exists(os.path.join(DOCS, "feed.xsl")):
        print("warning: docs/feed.xsl is missing; feed URLs will render as raw XML", file=sys.stderr)
    print(("Wrote %s" % DOCS) if changed else "No content change; files left alone.")
    if abandoned:
        # A fetch thread is still blocked on a socket somewhere. Its pool is not
        # daemonised, so returning normally would hang the interpreter after all
        # the work is safely on disk.
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
