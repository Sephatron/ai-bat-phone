"""Offline tests. No network — every case is built from a fixture.

Run: python3 -m unittest -v

Most of these exist because an adversarial review found the bug first. Where
that is so, the test is named for the failure it prevents rather than for the
function it calls.
"""

import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import adapters
import collect
import copywriter
import feedgen

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


def _parse_attributes(markup):
    """Yield (tag, [attribute names]) as a browser would see them."""
    from html.parser import HTMLParser

    found = []

    class Reader(HTMLParser):
        def handle_starttag(self, tag, attrs):
            found.append((tag, [name for name, _ in attrs]))

    reader = Reader()
    reader.feed(markup)
    return found
BEAT = collect.heartbeat_of(NOW)


def incident(**kw):
    defaults = dict(
        provider_key="acme",
        provider_name="Acme",
        incident_id="i1",
        title="Elevated errors",
        status="investigating",
        impact="major",
        kind="incident",
        url="https://status.acme.test/incidents/i1",
        started_at=NOW - timedelta(minutes=10),
        updated_at=NOW - timedelta(minutes=5),
        components=["API"],
        latest_update="We are looking into it.",
    )
    defaults.update(kw)
    return adapters.Incident(**defaults)


def event(**kw):
    defaults = dict(
        id="acme:i1:opened:investigating:major:x",
        provider_key="acme",
        provider_name="Acme",
        incident_id="i1",
        transition="opened",
        kind="incident",
        status="investigating",
        impact="major",
        title="Elevated errors",
        url="https://status.acme.test/incidents/i1",
        components=["API"],
        body="We are looking into it.",
        duration="",
        started_at=NOW.isoformat(),
        published_at=NOW.isoformat(),
        headline="🔴 Acme has downed tools",
    )
    defaults.update(kw)
    return defaults


class Classify(unittest.TestCase):
    def test_first_sight_of_a_live_incident_opens(self):
        self.assertEqual(collect.classify(incident(), None, NOW), "opened")

    def test_first_sight_of_a_finished_incident_is_silent(self):
        self.assertIsNone(collect.classify(incident(status="resolved"), None, NOW))

    def test_first_sight_of_an_untouched_open_incident_is_silent(self):
        stale = incident(started_at=NOW - timedelta(days=20), updated_at=NOW - timedelta(days=20))
        self.assertIsNone(collect.classify(stale, None, NOW))

    def test_first_sight_of_an_ancient_incident_is_silent(self):
        ancient = incident(started_at=NOW - timedelta(days=200), updated_at=NOW - timedelta(minutes=1))
        self.assertIsNone(collect.classify(ancient, None, NOW))

    def test_status_advance_is_progress(self):
        prev = {"status": "investigating", "impact": "major"}
        self.assertEqual(collect.classify(incident(status="identified"), prev, NOW), "progress")

    def test_impact_rise_is_escalation(self):
        prev = {"status": "investigating", "impact": "minor"}
        self.assertEqual(collect.classify(incident(impact="major"), prev, NOW), "escalated")

    def test_impact_fall_is_not_an_event(self):
        prev = {"status": "investigating", "impact": "critical"}
        self.assertIsNone(collect.classify(incident(impact="minor"), prev, NOW))

    def test_resolution_fires_once(self):
        prev = {"status": "monitoring", "impact": "major"}
        self.assertEqual(collect.classify(incident(status="resolved"), prev, NOW), "resolved")
        self.assertIsNone(
            collect.classify(incident(status="resolved"), {"status": "resolved", "impact": "major"}, NOW)
        )

    def test_no_change_is_silent(self):
        prev = {"status": "investigating", "impact": "major"}
        self.assertIsNone(collect.classify(incident(), prev, NOW))

    def test_a_reopened_incident_has_its_own_transition(self):
        prev = {"status": "resolved", "impact": "major"}
        self.assertEqual(collect.classify(incident(status="investigating"), prev, NOW), "reopened")


class ReopenAndFlap(unittest.TestCase):
    """The two ways an outage most often ends badly.

    Each used to collapse onto one event id, so the second resolution was
    dropped as a duplicate and the feed's last word stayed "down" while the
    provider was back up.

    The earlier version of this test gave every step its own `updated_at`, which
    made the ids unique by timestamp alone — it passed with the rest of the id
    scheme removed. These freeze what the provider reports and vary only what
    the provider actually changed.
    """

    def _ids(self, sequence, tracked=True, frozen=True):
        seen, ids = {}, []
        for offset, (status, impact) in enumerate(sequence):
            at = NOW if frozen else NOW + timedelta(minutes=offset)
            live = incident(status=status, impact=impact, updated_at=at, updates_tracked=tracked)
            transition = collect.classify(live, seen.get(live.key), NOW + timedelta(minutes=offset))
            if transition:
                ids.append(collect.make_event(live, transition, NOW + timedelta(minutes=offset))["id"])
            seen[live.key] = {"status": status, "impact": impact}
        return ids

    REOPEN = [("investigating", "major"), ("resolved", "major"),
              ("investigating", "major"), ("resolved", "major")]
    FLAP = [("investigating", "major"), ("monitoring", "major"), ("identified", "major"),
            ("monitoring", "major"), ("resolved", "major")]

    def test_a_second_resolution_after_a_reopen_is_published(self):
        ids = self._ids(self.REOPEN, frozen=False)
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 4, "ids collided, so an event would be dropped")

    def test_a_flapping_incident_reports_every_move(self):
        self.assertEqual(len(set(self._ids(self.FLAP, frozen=False))), 5)

    def test_it_still_holds_when_the_provider_timestamp_never_moves(self):
        """History feeds stamp every entry with the incident's start time.

        Keying the id on `updated_at` there restores the exact collision it was
        added to prevent, silently, for three of the watched providers.
        """
        ids = self._ids(self.REOPEN, tracked=False, frozen=True)
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 4, "the id degenerated for a history-feed source")

    def test_the_id_scheme_is_load_bearing(self):
        """A guard on the guard.

        Replace the id with one keyed only on the provider's update time — the
        scheme this project shipped before the fix — and the history-feed case
        above must collapse. If it does not, that test is proving nothing.
        """
        original = collect.make_event
        try:
            collect.make_event = lambda inc, tr, now: {"id": "%s:%s" % (inc.key, inc.updated_at)}
            ids = self._ids(self.REOPEN, tracked=False, frozen=True)
            self.assertLess(len(set(ids)), len(ids))
        finally:
            collect.make_event = original

    def test_successive_escalations_stay_distinct(self):
        first = collect.make_event(incident(impact="major"), "escalated", NOW)
        second = collect.make_event(incident(impact="critical"), "escalated", NOW)
        self.assertNotEqual(first["id"], second["id"])


class State(unittest.TestCase):
    def test_pruning_keys_on_our_sighting_not_the_providers_timestamp(self):
        """A long-running outage nobody updates is still live.

        Pruning on the provider's updated_at evicted incidents that were still
        on their status page, so the all-clear was never published.
        """
        seen = {
            "acme:i1": {
                "status": "investigating",
                "updated_at": (NOW - timedelta(days=120)).isoformat(),
                "last_seen_at": NOW.isoformat(),
            }
        }
        self.assertIn("acme:i1", collect.prune_state(seen, NOW))

    def test_genuinely_forgotten_incidents_are_dropped(self):
        seen = {"acme:old": {"last_seen_at": (NOW - timedelta(days=120)).isoformat()}}
        self.assertEqual(collect.prune_state(seen, NOW), {})

    def test_malformed_json_stops_the_run(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "state.json")
            with open(path, "w") as handle:
                handle.write('{"seen": {"a":')
            with self.assertRaises(collect.CorruptState):
                collect.load_json(path, {"seen": {}})

    def test_valid_json_of_the_wrong_shape_also_stops_the_run(self):
        """This was the dangerous case: it parsed, so it looked like a first
        run, and a first run re-announces every open incident at once."""
        for broken in (
            {"version": 2, "providers": {}},            # no seen key at all
            {"version": 2, "seen": [], "providers": {}},
            {"version": 2, "seen": {"a": "nope"}, "providers": {}},
            {"version": 99, "seen": {}, "providers": {}},
            ["not", "an", "object"],
        ):
            with self.subTest(broken=broken):
                with self.assertRaises(collect.CorruptState):
                    collect.validate_state(broken, "state.json")

    def test_a_healthy_state_file_passes(self):
        collect.validate_state({"version": 2, "seen": {}, "providers": {}}, "state.json")

    def test_a_missing_state_file_is_a_first_run(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(collect.load_json(os.path.join(folder, "nope.json"), {"x": 1}), {"x": 1})

    def test_writes_are_atomic_and_skip_unchanged_content(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "f.txt")
            self.assertTrue(collect.write_text(path, "hello"))
            self.assertFalse(collect.write_text(path, "hello"))
            self.assertTrue(collect.write_text(path, "goodbye"))
            self.assertEqual(os.listdir(folder), ["f.txt"], "temp file left behind")

    def test_the_heartbeat_is_hour_quantised(self):
        """Full resolution would commit every ten minutes; none at all would
        make a dead collector look like a quiet week."""
        self.assertEqual(collect.heartbeat_of(NOW.replace(minute=7, second=42)), NOW)
        self.assertEqual(collect.heartbeat_of(NOW.replace(minute=59)), NOW)


class ProviderHealth(unittest.TestCase):
    provider = {"key": "acme", "name": "Acme", "base": "https://status.acme.test"}

    def test_a_blip_says_nothing_but_a_run_of_failures_does(self):
        tracked = {}
        for _ in range(collect.FAILURES_BEFORE_ALARM - 1):
            self.assertEqual(collect.track_providers([self.provider], {"acme": "timeout"}, tracked, NOW), [])
        events = collect.track_providers([self.provider], {"acme": "timeout"}, tracked, NOW)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["kind"], "meta")
        self.assertIn("unreadable", events[0]["headline"])

    def test_it_alarms_once_not_every_run(self):
        tracked = {}
        for _ in range(10):
            collect.track_providers([self.provider], {"acme": "timeout"}, tracked, NOW)
        extra = collect.track_providers([self.provider], {"acme": "timeout"}, tracked, NOW)
        self.assertEqual(extra, [])

    def test_recovery_is_announced(self):
        tracked = {}
        for _ in range(collect.FAILURES_BEFORE_ALARM):
            collect.track_providers([self.provider], {"acme": "timeout"}, tracked, NOW)
        events = collect.track_providers([self.provider], {}, tracked, NOW)
        self.assertEqual(len(events), 1)
        self.assertIn("readable again", events[0]["headline"])

    def test_a_transient_failure_does_not_reach_the_page(self):
        """Rendering every blip rewrote index.html and forced a commit, then
        another when it cleared."""
        tracked = {}
        collect.track_providers([self.provider], {"acme": "timeout"}, tracked, NOW)
        self.assertEqual(collect.unreachable_now([self.provider], tracked), {})

    def test_a_sustained_failure_does(self):
        tracked = {}
        for _ in range(collect.FAILURES_BEFORE_ALARM):
            collect.track_providers([self.provider], {"acme": "timeout"}, tracked, NOW)
        self.assertEqual(collect.unreachable_now([self.provider], tracked), {"Acme": "timeout"})


class UntrustedInput(unittest.TestCase):
    """Everything here arrives from a third party's status page."""

    def test_control_characters_are_stripped_before_they_reach_a_feed(self):
        for label, char in (("NUL", "\x00"), ("VT", "\x0b"), ("FF", "\x0c"), ("ESC", "\x1b")):
            with self.subTest(char=label):
                built = incident(title="Outage%stitle" % char)
                xml = feedgen.build_rss([collect.make_event(built, "opened", NOW)], "feed.xml", BEAT)
                ET.fromstring(xml)  # raises if the feed is malformed
                self.assertNotIn(char, xml)

    def test_lone_surrogates_do_not_crash_the_writer(self):
        """Through the real sink. json.dumps defaults to ensure_ascii=True, which
        never raises on a surrogate, so the earlier version of this test passed
        with the surrogate range deleted from the scrubber."""
        built = incident(title="Outage \ud800 here")
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "events.json")
            collect.write_json(path, [collect.make_event(built, "opened", NOW)])
            self.assertTrue(os.path.exists(path))

    def test_a_javascript_url_never_reaches_the_page(self):
        built = incident(url="javascript:alert(document.domain)")
        self.assertTrue(built.url.startswith("https://"))

    def test_a_quoted_url_cannot_break_out_of_an_attribute(self):
        """saxutils.escape leaves quotes alone, so href="..." was an XSS sink.

        Checked by parsing the result and looking at the attributes the browser
        would actually see, not by grepping the string.
        """
        hostile = event(url='https://s.test/a" onmouseover="alert(1)" x="')
        html = feedgen.build_index([hostile], [], {}, BEAT)
        for tag, attrs in _parse_attributes(html):
            with self.subTest(tag=tag):
                self.assertFalse(
                    [name for name in attrs if name.startswith("on")],
                    "event handler attribute injected into <%s>" % tag,
                )

    def test_a_hostile_url_is_dropped_before_it_is_ever_rendered(self):
        built = incident(url='https://s.test/a" onmouseover="alert(1)" x="')
        self.assertNotIn('"', built.url)

    def test_the_same_url_is_safe_inside_a_feed_body(self):
        hostile = event(url='https://s.test/a" onmouseover="alert(1)" x="')
        xml = feedgen.build_rss([hostile], "feed.xml", BEAT)
        ET.fromstring(xml)
        body = ET.fromstring(xml).findtext(".//channel/item/description")
        for tag, attrs in _parse_attributes(body):
            self.assertFalse([name for name in attrs if name.startswith("on")])

    def test_titles_are_bounded(self):
        self.assertLessEqual(len(incident(title="x" * 5000).title), adapters.MAX_TITLE)

    def test_a_cdata_terminator_cannot_break_the_feed(self):
        xml = feedgen.build_rss([event(title="oops ]]> here")], "feed.xml", BEAT)
        ET.fromstring(xml)


class RssClassifier(unittest.TestCase):
    recognised = [
        ("[Resolved] We have resolved the issue.", ("incident", "resolved")),
        ("[Monitoring] A fix has been applied.", ("incident", "monitoring")),
        ("[Mitigated] Traffic has been shifted.", ("incident", "monitoring")),
        ("[Scheduled] We'll be performing database maintenance.", ("maintenance", "scheduled")),
        ("[Completed] The maintenance is complete.", ("maintenance", "completed")),
        ("Status: resolved The incident has been resolved.", ("incident", "resolved")),
        ("Type: Incident Duration: 40 minutes Affected Components: API", ("incident", "resolved")),
        ("Type: Maintenance Duration: 20 minutes", ("maintenance", "completed")),
        ("Type: Incident We are seeing elevated error rates.", ("incident", "investigating")),
    ]

    def test_recognised_dialects(self):
        for text, expected in self.recognised:
            with self.subTest(text=text[:30]):
                self.assertEqual(adapters._rss_classify(text), expected)

    def test_a_live_outage_is_not_read_as_resolved(self):
        """A substring test for "resolved" called this an all-clear and dropped
        the outage entirely."""
        text = "We are continuing to work on a fix. The issue is not yet resolved."
        self.assertEqual(adapters._rss_classify(text), ("incident", "investigating"))

    def test_an_unrecognised_dialect_returns_none_rather_than_guessing(self):
        self.assertIsNone(adapters._rss_classify("[Kerfuffle] Something happened."))
        self.assertIsNone(adapters._rss_classify("Status: bewildered"))
        self.assertIsNone(adapters._rss_classify("The API returned to normal operation."))

    def test_the_status_line_does_not_swallow_the_prose_after_it(self):
        match = adapters._RSS_STATUS_LINE.search("Status: resolved The incident has been resolved.")
        self.assertEqual(match.group(1), "resolved")


class Copy(unittest.TestCase):
    def test_is_deterministic(self):
        self.assertEqual(copywriter.headline(incident(), "opened"), copywriter.headline(incident(), "opened"))

    def test_security_and_data_incidents_get_no_joke(self):
        for title in (
            "Customer accounts may have been compromised",
            "A vulnerability was found in our auth layer",
            "Security incident affecting logins",
            "Some customer data was deleted",
            "Database corruption affecting a subset of projects",
            "Credentials rotated following an incident",
        ):
            with self.subTest(title=title):
                line = copywriter.headline(incident(title=title), "opened")
                self.assertIn("⚠️", line, "joked about %r" % title)
                self.assertIn(title, line)

    def test_every_template_formats_cleanly(self):
        pools = (
            copywriter.OPENED_HIGH + copywriter.OPENED_LOW + copywriter.ESCALATED
            + copywriter.IDENTIFIED + copywriter.MONITORING + copywriter.REOPENED
            + copywriter.RESOLVED_HIGH + copywriter.RESOLVED_LOW
            + [t for pool in copywriter.MAINTENANCE.values() for t in pool]
            + [t for pool in copywriter.META.values() for t in pool]
        )
        for template in pools:
            with self.subTest(template=template):
                self.assertNotIn("{", template.format(p="Acme", title="X", impact="major", duration="1h"))

    def test_a_first_sighting_at_monitoring_does_not_read_as_breaking(self):
        self.assertIn("👀", copywriter.headline(incident(status="monitoring"), "opened"))

    def test_a_critical_incident_never_wears_the_minor_badge(self):
        """Colour is the fastest signal in a reader's list; the fallback branch
        hardcoded orange regardless of impact."""
        line = copywriter.headline(incident(impact="critical", status="investigating"), "progress")
        self.assertIn("🔴", line)

    def test_monitor_failures_are_never_a_joke(self):
        line = copywriter.meta_headline("Claude", "unreachable")
        self.assertIn("Claude", line)
        self.assertIn("silence", line.lower())

    def test_durations(self):
        self.assertEqual(copywriter.format_duration(timedelta(minutes=42)), "42 min")
        self.assertEqual(copywriter.format_duration(timedelta(hours=1, minutes=4)), "1h 04m")
        self.assertEqual(copywriter.format_duration(timedelta(days=2, hours=3)), "2d 3h")


class EventShape(unittest.TestCase):
    def test_items_are_stamped_with_when_we_noticed(self):
        """A backdated all-clear sorts below everything already read."""
        old = incident(status="resolved", started_at=NOW - timedelta(days=3), updated_at=NOW - timedelta(days=2))
        self.assertEqual(collect.make_event(old, "resolved", NOW)["published_at"], NOW.isoformat())

    def test_the_real_start_time_is_still_reported(self):
        old = incident(started_at=NOW - timedelta(hours=5))
        built = collect.make_event(old, "opened", NOW)
        self.assertEqual(built["started_at"], (NOW - timedelta(hours=5)).isoformat())
        self.assertIn("Opened:", feedgen._item_body(built))

    def test_resolution_carries_a_duration(self):
        done = incident(status="resolved", started_at=NOW - timedelta(hours=1), updated_at=NOW)
        self.assertEqual(collect.make_event(done, "resolved", NOW)["duration"], "1h 00m")

    def test_no_duration_is_claimed_for_sources_that_cannot_measure_one(self):
        """History feeds stamp every entry with the incident's start time, so
        updated_at - started_at is always zero and 'under a minute' was a lie."""
        done = incident(status="resolved", updates_tracked=False, updated_at=incident().started_at)
        built = collect.make_event(done, "resolved", NOW)
        self.assertEqual(built["duration"], "")
        self.assertNotIn("under a minute", built["headline"])


class Feed(unittest.TestCase):
    def test_variants_filter(self):
        events = [
            event(),
            event(id="b", kind="maintenance", impact="none"),
            event(id="c", impact="minor"),
        ]
        self.assertEqual(len(self._items("feed.xml", events)), 3)
        self.assertEqual(len(self._items("outages.xml", events)), 2)
        self.assertEqual(len(self._items("major.xml", events)), 1)

    def test_monitor_failures_appear_in_every_variant(self):
        """A major-only subscriber most needs to know when we have gone blind."""
        meta = [event(id="m", kind="meta", impact="none", status="unreachable")]
        for filename in feedgen.VARIANTS:
            with self.subTest(filename=filename):
                self.assertEqual(len(self._items(filename, meta)), 1)

    def test_last_build_date_tracks_the_poll_not_the_publish(self):
        xml = feedgen.build_rss([], "feed.xml", BEAT)
        root = ET.fromstring(xml)
        self.assertIn("12:00:00", root.findtext(".//channel/lastBuildDate"))

    def test_guid_is_not_a_permalink(self):
        self.assertIn("isPermaLink='false'", feedgen.build_rss([event()], "feed.xml", BEAT))

    def test_impact_none_is_not_shown_to_a_reader_as_none(self):
        body = feedgen._item_body(event(impact="none"))
        self.assertIn("unclassified", body)
        self.assertNotIn("Impact: none", body)

    def test_index_renders_without_events(self):
        html = feedgen.build_index([], [{"name": "Acme", "adapter": "statuspage"}], {}, BEAT)
        self.assertIn("Nothing yet", html)
        self.assertIn("last checked", html)

    def test_index_names_providers_it_cannot_read(self):
        html = feedgen.build_index([], [], {"Acme": "timed out"}, BEAT)
        self.assertIn("timed out", html)
        self.assertIn("means nothing either way", html)

    def test_index_names_providers_it_does_not_watch_at_all(self):
        html = feedgen.build_index([], [], {}, BEAT, gaps=[{"name": "Mistral", "note": "no endpoint"}])
        self.assertIn("Mistral", html)
        self.assertIn("Not watched", html)

    def test_emoji_is_split_from_the_words_for_the_page(self):
        self.assertEqual(feedgen._split_badge("🟠 Acme wobbles"), ("🟠", "Acme wobbles"))
        self.assertEqual(feedgen._split_badge("Acme: plain"), ("", "Acme: plain"))
        self.assertEqual(feedgen._split_badge("⚠️"), ("", "⚠️"))

    def _items(self, filename, events):
        return ET.fromstring(feedgen.build_rss(events, filename, BEAT)).findall(".//channel/item")


class SoberBoundaries(unittest.TestCase):
    """Both directions, because two earlier versions failed in opposite ones.

    Add to ORDINARY before adding a pattern to copywriter.SOBER.
    """

    ORDINARY = [
        "We have identified the root cause and are rolling out a fix",
        "Elevated Build Errors for Secure Compute/Static IPs Projects",
        "Scheduled maintenance: security patching of API gateway",
        "A memory leak in the router caused elevated latency",
        "A misconfiguration deleted the cache and caused cold starts",
        "This exposed a latent bug in the scheduler",
        "Postmortem published for the 14 August incident",
        "Some rows were corrupt after the migration",
        "Elevated error rates on the completions endpoint",
        "Incident with Actions",
        "Increased API Error Rates",
    ]
    SERIOUS = [
        "Some user conversations were visible to other users",
        "Customer API keys may have been viewable in the dashboard",
        "Notice of a data incident affecting a subset of customers",
        "We are investigating a potential exposure of customer metadata",
        "Ransomware event at a third-party vendor",
        "PII was inadvertently included in log output",
        "Unauthorised access to an internal tool",
        "Customer accounts may have been compromised",
        "Security incident affecting the analytics pipeline",
        "CVE-2026-1234 in our ingress layer",
        "Database corruption affecting a subset of projects",
    ]

    def test_ordinary_outages_keep_their_joke(self):
        for title in self.ORDINARY:
            with self.subTest(title=title):
                self.assertNotIn("⚠️", copywriter.headline(incident(title=title), "opened"))

    def test_serious_incidents_lose_it(self):
        for title in self.SERIOUS:
            with self.subTest(title=title):
                line = copywriter.headline(incident(title=title), "opened")
                self.assertIn("⚠️", line)
                self.assertIn(title, line)

    def test_tone_does_not_flip_when_the_body_changes(self):
        """The body is the rolling latest update, so matching on it made an
        incident change voice halfway through."""
        one = copywriter.headline(incident(latest_update="Investigating."), "opened")
        two = copywriter.headline(incident(latest_update="We have found the root cause."), "opened")
        self.assertEqual(one, two)

    def test_planned_security_work_is_not_an_alarm(self):
        line = copywriter.headline(
            incident(kind="maintenance", status="scheduled", title="Security patching of the API gateway"),
            "opened",
        )
        self.assertIn("🔧", line)


class MetaEventSafety(unittest.TestCase):
    """make_meta_event is the one path to a feed that skips the Incident
    constructor, which is where the scrubbing lives. It broke all three feeds."""

    provider = {"key": "acme", "name": "Acme", "base": "https://status.acme.test"}

    def test_a_servers_own_bytes_cannot_break_the_feed(self):
        hostile = "http://x/api: OO\x0cPS junk\r\n"
        built = collect.make_meta_event(self.provider, "unreachable", NOW.isoformat(), hostile, NOW)
        for filename in feedgen.VARIANTS:
            with self.subTest(filename=filename):
                ET.fromstring(feedgen.build_rss([built], filename, BEAT))

    def test_the_error_text_is_bounded(self):
        built = collect.make_meta_event(self.provider, "unreachable", NOW.isoformat(), "x" * 70000, NOW)
        self.assertLessEqual(len(built["body"]), 300)

    def test_the_provider_base_is_still_url_checked(self):
        bad = dict(self.provider, base="javascript:alert(1)")
        built = collect.make_meta_event(bad, "unreachable", NOW.isoformat(), "", NOW)
        self.assertFalse(built["url"].startswith("javascript:"))


class RenderTimeSafety(unittest.TestCase):
    """events.json outlives the code that wrote it, so validating once at
    capture time makes the invariant depend on every future writer."""

    def test_a_javascript_url_in_the_stored_log_is_not_rendered(self):
        stored = event(url="javascript:alert(document.domain)")
        html = feedgen.build_index([stored], [], {}, BEAT)
        self.assertNotIn("javascript:", html)
        self.assertNotIn("javascript:", feedgen.build_rss([stored], "feed.xml", BEAT))

    def test_a_headline_with_markup_does_not_reach_a_reader_as_markup(self):
        stored = event(headline="⚠️ Acme: Security review <img src=x onerror=alert(1)>")
        xml = feedgen.build_rss([stored], "feed.xml", BEAT)
        title = ET.fromstring(xml).findtext(".//channel/item/title")
        self.assertNotIn("<img", title)

    def test_a_literal_percent_in_page_copy_does_not_crash_the_job(self):
        """The page was one %-format string; "100% of the roster" killed it."""
        original = feedgen.PAGE
        try:
            feedgen.PAGE = feedgen.Template(
                original.template.replace("<h2>Watching</h2>", "<h2>Watching (100% of the roster)</h2>")
            )
            self.assertIn("100% of the roster", feedgen.build_index([], [], {}, BEAT))
        finally:
            feedgen.PAGE = original

    def test_the_monitoring_badge_is_not_internal_vocabulary(self):
        html = feedgen.build_index([event(kind="meta", impact="none")], [], {}, BEAT)
        self.assertIn("monitoring", html)
        self.assertNotIn(">meta<", html)


class AdapterShapeGuards(unittest.TestCase):
    def test_an_unknown_status_word_is_skipped_not_coerced(self):
        """Coercing it to a live status turned every resolved incident into a
        reopening the moment a status host added a vocabulary word."""
        raw = {
            "id": "1", "name": "n", "status": "closed", "impact": "major",
            "created_at": "2026-09-01T00:00:00Z", "updated_at": "2026-09-01T00:00:00Z",
        }
        self.assertIsNone(
            adapters._statuspage_incident({"key": "x", "name": "X"}, raw, "https://s.test")
        )

    def test_a_missing_incidents_key_is_a_shape_change(self):
        original = adapters._get_json
        try:
            adapters._get_json = lambda url: {"incidents_v3": []}
            with self.assertRaises(adapters.FetchError):
                adapters.fetch_statuspage({"key": "x", "name": "X", "base": "https://s.test"})
        finally:
            adapters._get_json = original

    def test_a_history_feed_that_yields_nothing_is_a_shape_change(self):
        """Returning [] would look like a calm provider and reset the failure
        counter — the one outcome this project must never produce."""
        feeds = {
            "atom": b"<feed xmlns='http://www.w3.org/2005/Atom'><entry><title>x</title></entry></feed>",
            "iso dates": b"<rss><channel><item><title>x</title><guid>g</guid>"
            b"<pubDate>2026-09-04T12:00:00Z</pubDate><description>[Resolved] x</description>"
            b"</item></channel></rss>",
            "empty": b"<rss><channel></channel></rss>",
        }
        original = adapters._get
        try:
            for label, body in feeds.items():
                with self.subTest(feed=label):
                    adapters._get = lambda url, accept, body=body: (200, body)
                    with self.assertRaises(adapters.FetchError):
                        adapters.fetch_rss({"key": "x", "name": "X", "base": "https://s.test"})
        finally:
            adapters._get = original

    def test_the_restored_and_monitoring_line_does_not_read_as_an_all_clear(self):
        text = ("Type: Incident Sep 4, 10:00 - Monitoring - Service has been restored "
                "for most users and we are monitoring.")
        self.assertEqual(adapters._rss_classify(text), ("incident", "investigating"))

    def test_xml_bombs_are_refused_by_the_parser(self):
        """The zero-dependency choice rests on expat's own limits rather than on
        defusedxml, so assert them instead of trusting the comment that
        describes them. If a runner ever ships expat < 2.4 this fails loudly."""
        entities = b"".join(
            b"<!ENTITY e%d '%s'>" % (n, (b"&e%d;" % (n - 1)) * 10) for n in range(1, 10)
        )
        bomb = (
            b"<?xml version='1.0'?><!DOCTYPE l [<!ENTITY e0 'aaaaaaaaaa'>"
            + entities
            + b"]><rss>&e9;</rss>"
        )
        with self.assertRaises(ET.ParseError):
            ET.fromstring(bomb)
        with self.assertRaises(ET.ParseError):
            ET.fromstring(
                b"<?xml version='1.0'?><!DOCTYPE l SYSTEM 'file:///etc/passwd'><rss>&xxe;</rss>"
            )


class Bookkeeping(unittest.TestCase):
    provider = {"key": "acme", "name": "Acme", "base": "https://status.acme.test"}

    def test_the_failure_counter_stops_growing(self):
        """An uncapped counter rewrote state.json every poll for as long as one
        provider was down — 24 commits a day became 144."""
        tracked = {}
        for _ in range(12):
            collect.track_providers([self.provider], {"acme": "timeout"}, tracked, NOW, BEAT)
        first = json.dumps(tracked, sort_keys=True)
        collect.track_providers([self.provider], {"acme": "timeout"}, tracked, NOW, BEAT)
        self.assertEqual(first, json.dumps(tracked, sort_keys=True))

    def test_a_removed_provider_does_not_linger(self):
        tracked = {}
        collect.track_providers([self.provider], {"acme": "timeout"}, tracked, NOW, BEAT)
        collect.track_providers([], {}, tracked, NOW, BEAT)
        self.assertEqual(tracked, {})

    def test_recovery_survives_a_record_with_no_failing_since(self):
        tracked = {"acme": {"consecutive_failures": collect.FAILURES_BEFORE_ALARM}}
        events = collect.track_providers([self.provider], {}, tracked, NOW, BEAT)
        self.assertEqual(len(events), 1)

    def test_the_seen_map_is_day_quantised(self):
        self.assertEqual(collect.day_of(NOW).hour, 0)

    def test_proof_of_life_is_due_when_the_last_one_has_aged_out(self):
        stale = [event(transition="alive", kind="meta",
                       published_at=(NOW - collect.ALIVE_EVERY - timedelta(hours=1)).isoformat())]
        self.assertIsNotNone(collect.alive_event(stale, [], NOW))

    def test_proof_of_life_is_not_due_yet(self):
        fresh = [event(transition="alive", kind="meta", published_at=NOW.isoformat())]
        self.assertIsNone(collect.alive_event(fresh, [], NOW))


class Registry(unittest.TestCase):
    def test_the_real_registry_loads(self):
        providers, gaps = collect.load_providers()
        self.assertGreater(len(providers), 15)
        self.assertTrue(all(p.get("note") for p in gaps), "a listed gap with no reason")

    def test_every_provider_uses_a_known_adapter(self):
        providers, gaps = collect.load_providers()
        for provider in providers + gaps:
            self.assertIn(provider["adapter"], adapters.ADAPTERS, provider["key"])

    def test_unreachable_gaps_are_marked_as_such(self):
        """Their adapter and base are guesses, so "flip enabled" is wrong advice
        for them and the page should not imply otherwise."""
        _, gaps = collect.load_providers()
        self.assertTrue(any(g.get("blocked") for g in gaps))
        for gap in gaps:
            with self.subTest(key=gap["key"]):
                self.assertIsInstance(gap.get("blocked", False), bool)


if __name__ == "__main__":
    unittest.main()


class EndToEnd(unittest.TestCase):
    """main() itself, with the network stubbed out.

    Nothing covered this before, which is how a churn-control design shipped
    twice without an end-to-end assertion that a quiet run writes nothing.
    """

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.paths = {}
        for name, filename in (
            ("STATE_FILE", "state.json"),
            ("EVENTS_FILE", "events.json"),
            ("DOCS", "docs"),
        ):
            self.paths[name] = getattr(collect, name)
            setattr(collect, name, os.path.join(self.folder.name, filename))
        self.real_fetch = adapters.fetch
        self.live = []
        adapters.fetch = lambda provider: [
            adapters.Incident(
                provider_key=provider["key"], provider_name=provider["name"],
                incident_id="i1", title="Elevated errors", status=status, impact="major",
                kind="incident", url="https://status.example.test/i1",
                started_at=NOW - timedelta(minutes=5), updated_at=NOW, components=[],
                latest_update="Looking into it.",
            )
            for status in self.live
        ]

    def tearDown(self):
        adapters.fetch = self.real_fetch
        for name, value in self.paths.items():
            setattr(collect, name, value)
        self.folder.cleanup()

    def _run(self):
        return collect.main([])

    def _snapshot(self):
        out = {}
        for root, _, files in os.walk(self.folder.name):
            for name in files:
                path = os.path.join(root, name)
                with open(path, "rb") as handle:
                    out[os.path.relpath(path, self.folder.name)] = handle.read()
        return out

    def test_a_quiet_second_run_writes_nothing(self):
        self.live = ["investigating"]
        self.assertEqual(self._run(), 0)
        before = self._snapshot()
        self.assertEqual(self._run(), 0)
        self.assertEqual(before, self._snapshot(), "a quiet run changed a file")

    def test_a_run_where_a_provider_is_failing_still_writes_nothing_extra(self):
        """The failure counter used to climb for ever, rewriting state.json on
        every poll for as long as the outage lasted."""
        self.live = ["investigating"]
        self._run()
        adapters.fetch = lambda provider: (_ for _ in ()).throw(adapters.FetchError("timeout"))
        for _ in range(collect.FAILURES_BEFORE_ALARM + 1):
            self._run()
        before = self._snapshot()
        self._run()
        self.assertEqual(before, self._snapshot(), "a steady failure kept churning files")

    def test_an_outage_is_announced_then_resolved(self):
        self.live = ["investigating"]
        self._run()
        self.live = ["resolved"]
        self._run()
        with open(collect.EVENTS_FILE) as handle:
            transitions = [e["transition"] for e in json.load(handle)]
        self.assertIn("opened", transitions)
        self.assertIn("resolved", transitions)

    def test_a_crash_between_the_two_state_writes_does_not_lose_the_event(self):
        """events.json is written first on purpose: the other order leaves an
        incident marked seen with no event ever published."""
        self.live = ["investigating"]
        self._run()
        self.live = ["resolved"]
        real_write = collect.write_json

        def die_after_events(path, payload):
            result = real_write(path, payload)
            if path == collect.STATE_FILE:
                raise KeyboardInterrupt("runner killed")
            return result

        collect.write_json = die_after_events
        try:
            with self.assertRaises(KeyboardInterrupt):
                self._run()
        finally:
            collect.write_json = real_write
        self.assertEqual(self._run(), 0)
        with open(collect.EVENTS_FILE) as handle:
            events = json.load(handle)
        resolutions = [e for e in events if e["transition"] == "resolved"]
        self.assertTrue(resolutions, "the resolution was lost")
        self.assertEqual(
            len(resolutions),
            len({e["provider_key"] for e in resolutions}),
            "the resolution was published twice",
        )

    def test_every_provider_failing_leaves_the_feed_untouched(self):
        self.live = ["investigating"]
        self._run()
        before = self._snapshot()
        adapters.fetch = lambda provider: (_ for _ in ()).throw(adapters.FetchError("network down"))
        self.assertEqual(self._run(), 1)
        self.assertEqual(before, self._snapshot(), "a total failure rewrote the feed")
