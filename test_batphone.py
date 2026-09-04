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

    Both used to collapse onto one event id, so the second resolution was
    dropped as a duplicate and the feed's last word stayed "down" while the
    provider was back up.
    """

    def _ids(self, sequence):
        seen, ids = {}, []
        for offset, (status, impact) in enumerate(sequence):
            live = incident(status=status, impact=impact, updated_at=NOW + timedelta(minutes=offset))
            transition = collect.classify(live, seen.get(live.key), NOW + timedelta(minutes=offset))
            if transition:
                ids.append(collect.make_event(live, transition, NOW)["id"])
            seen[live.key] = {"status": status, "impact": impact}
        return ids

    def test_a_second_resolution_after_a_reopen_is_published(self):
        ids = self._ids(
            [
                ("investigating", "major"),
                ("resolved", "major"),
                ("investigating", "major"),
                ("resolved", "major"),
            ]
        )
        self.assertEqual(len(ids), 4)
        self.assertEqual(len(set(ids)), 4, "ids collided, so an event would be dropped")

    def test_a_flapping_incident_reports_every_move(self):
        ids = self._ids(
            [
                ("investigating", "major"),
                ("monitoring", "major"),
                ("identified", "major"),
                ("monitoring", "major"),
                ("resolved", "major"),
            ]
        )
        self.assertEqual(len(set(ids)), 5)

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

    def test_a_corrupt_state_file_stops_the_run(self):
        """It must not look like a first run — that re-announces everything."""
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "state.json")
            with open(path, "w") as handle:
                handle.write('{"seen": {"a":')
            with self.assertRaises(collect.CorruptState):
                collect.load_json(path, {"seen": {}})

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
        self.assertIn("Cannot reach", events[0]["headline"])

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
        built = incident(title="Outage \ud800 here")
        json.dumps(collect.make_event(built, "opened", NOW))  # would raise on a surrogate

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


class Config(unittest.TestCase):
    def test_the_real_registry_loads(self):
        providers, gaps = collect.load_providers()
        self.assertGreater(len(providers), 10)
        self.assertTrue(all(p.get("note") for p in gaps), "a documented gap with no reason")

    def test_every_provider_uses_a_known_adapter(self):
        providers, gaps = collect.load_providers()
        for provider in providers + gaps:
            self.assertIn(provider["adapter"], adapters.ADAPTERS, provider["key"])


if __name__ == "__main__":
    unittest.main()
