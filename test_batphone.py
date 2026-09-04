"""Offline tests. No network — every case is built from a fixture.

Run: python3 -m unittest -v
"""

import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import adapters
import collect
import copywriter
import feedgen

NOW = datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)


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


class Classify(unittest.TestCase):
    def test_first_sight_of_a_live_incident_opens(self):
        self.assertEqual(collect.classify(incident(), None, False, NOW), "opened")

    def test_first_sight_of_a_finished_incident_is_silent(self):
        self.assertIsNone(collect.classify(incident(status="resolved"), None, False, NOW))

    def test_first_sight_of_an_untouched_open_incident_is_silent(self):
        stale = incident(started_at=NOW - timedelta(days=20), updated_at=NOW - timedelta(days=20))
        self.assertIsNone(collect.classify(stale, None, False, NOW))

    def test_first_sight_of_an_ancient_incident_is_silent(self):
        ancient = incident(started_at=NOW - timedelta(days=200), updated_at=NOW - timedelta(minutes=1))
        self.assertIsNone(collect.classify(ancient, None, False, NOW))

    def test_status_advance_is_progress(self):
        prev = {"status": "investigating", "impact": "major"}
        self.assertEqual(collect.classify(incident(status="identified"), prev, False, NOW), "progress")

    def test_impact_rise_is_escalation(self):
        prev = {"status": "investigating", "impact": "minor"}
        self.assertEqual(collect.classify(incident(impact="major"), prev, False, NOW), "escalated")

    def test_impact_fall_is_not_an_event(self):
        prev = {"status": "investigating", "impact": "critical"}
        self.assertIsNone(collect.classify(incident(impact="minor"), prev, False, NOW))

    def test_resolution_fires_once(self):
        prev = {"status": "monitoring", "impact": "major"}
        resolved = incident(status="resolved")
        self.assertEqual(collect.classify(resolved, prev, False, NOW), "resolved")
        self.assertIsNone(collect.classify(resolved, {"status": "resolved", "impact": "major"}, False, NOW))

    def test_no_change_is_silent(self):
        prev = {"status": "investigating", "impact": "major"}
        self.assertIsNone(collect.classify(incident(), prev, False, NOW))

    def test_bootstrap_announces_live_but_not_transitions(self):
        self.assertEqual(collect.classify(incident(), None, True, NOW), "opened")
        prev = {"status": "investigating", "impact": "major"}
        self.assertIsNone(collect.classify(incident(status="resolved"), prev, True, NOW))


class RssClassifier(unittest.TestCase):
    cases = [
        ("[Resolved] We have resolved the issue.", ("incident", "resolved")),
        ("[Monitoring] A fix has been applied.", ("incident", "monitoring")),
        ("[Scheduled] We'll be performing database maintenance.", ("maintenance", "scheduled")),
        ("[Completed] The maintenance is complete.", ("maintenance", "completed")),
        ("Status: resolved The incident has been resolved.", ("incident", "resolved")),
        ("Type: Incident Duration: 40 minutes Affected Components: API", ("incident", "resolved")),
        ("Type: Maintenance Duration: 20 minutes", ("maintenance", "completed")),
        ("We are seeing elevated error rates.", ("incident", "investigating")),
    ]

    def test_markers(self):
        for text, expected in self.cases:
            with self.subTest(text=text[:30]):
                self.assertEqual(adapters._rss_classify(text), expected)


class Copy(unittest.TestCase):
    def test_is_deterministic(self):
        one = copywriter.headline(incident(), "opened")
        two = copywriter.headline(incident(), "opened")
        self.assertEqual(one, two)

    def test_security_incidents_get_no_joke(self):
        line = copywriter.headline(incident(title="Security incident: credentials exposed"), "opened")
        self.assertIn("Acme", line)
        self.assertIn("credentials exposed", line)

    def test_every_template_formats_cleanly(self):
        pools = (
            copywriter.OPENED_HIGH + copywriter.OPENED_LOW + copywriter.ESCALATED
            + copywriter.IDENTIFIED + copywriter.MONITORING
            + copywriter.RESOLVED_HIGH + copywriter.RESOLVED_LOW
            + [t for pool in copywriter.MAINTENANCE.values() for t in pool]
        )
        for template in pools:
            with self.subTest(template=template):
                rendered = template.format(p="Acme", title="X", impact="major", duration="1h")
                self.assertNotIn("{", rendered)

    def test_a_first_sighting_at_monitoring_does_not_read_as_breaking(self):
        line = copywriter.headline(incident(status="monitoring"), "opened")
        self.assertIn("👀", line)

    def test_durations(self):
        self.assertEqual(copywriter.format_duration(timedelta(minutes=42)), "42 min")
        self.assertEqual(copywriter.format_duration(timedelta(hours=1, minutes=4)), "1h 04m")
        self.assertEqual(copywriter.format_duration(timedelta(days=2, hours=3)), "2d 3h")


def event(**kw):
    defaults = dict(
        id="acme:i1:opened:investigating:major",
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
        published_at=NOW.isoformat(),
        headline="🔴 Acme has downed tools",
    )
    defaults.update(kw)
    return defaults


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

    def test_cdata_terminator_cannot_break_the_feed(self):
        xml = feedgen.build_rss([event(title="oops ]]> here")], "feed.xml", "https://x.test")
        ET.fromstring(xml)  # raises if the CDATA section closed early
        self.assertNotIn("]]> here", xml)

    def test_control_characters_in_a_title_still_parse(self):
        xml = feedgen.build_rss([event(headline="a & b < c > d \"e\"")], "feed.xml", "https://x.test")
        ET.fromstring(xml)

    def test_guid_is_not_a_permalink(self):
        xml = feedgen.build_rss([event()], "feed.xml", "https://x.test")
        self.assertIn('isPermaLink="false"', xml)

    def test_emoji_is_split_from_the_words_for_the_page(self):
        self.assertEqual(feedgen._split_badge("🟠 Acme wobbles"), ("🟠", "Acme wobbles"))
        self.assertEqual(feedgen._split_badge("Acme: plain"), ("", "Acme: plain"))
        self.assertEqual(feedgen._split_badge("⚠️"), ("", "⚠️"))

    def test_index_renders_without_events(self):
        html = feedgen.build_index([], [{"name": "Acme", "adapter": "statuspage"}], {}, "https://x.test")
        self.assertIn("Nothing yet", html)

    def test_index_shows_unreachable_providers(self):
        html = feedgen.build_index([], [], {"Acme": "timed out"}, "https://x.test")
        self.assertIn("timed out", html)
        self.assertIn("never reported as recovered", html)

    def _items(self, filename, events):
        root = ET.fromstring(feedgen.build_rss(events, filename, "https://x.test"))
        return root.findall(".//channel/item")


class Writing(unittest.TestCase):
    def test_build_stamps_do_not_count_as_a_change(self):
        a = "<lastBuildDate>Fri, 04 Sep 2026 11:00:00 +0000</lastBuildDate><x>1</x>"
        b = "<lastBuildDate>Fri, 04 Sep 2026 12:00:00 +0000</lastBuildDate><x>1</x>"
        self.assertEqual(collect._stable(a), collect._stable(b))

    def test_real_changes_still_count(self):
        a = "<lastBuildDate>t</lastBuildDate><x>1</x>"
        b = "<lastBuildDate>t</lastBuildDate><x>2</x>"
        self.assertNotEqual(collect._stable(a), collect._stable(b))


class EventShape(unittest.TestCase):
    def test_future_timestamps_are_clamped(self):
        future = incident(updated_at=NOW + timedelta(hours=6), status="identified")
        built = collect.make_event(future, "progress", NOW)
        self.assertEqual(built["published_at"], NOW.isoformat())

    def test_resolution_carries_a_duration(self):
        done = incident(status="resolved", started_at=NOW - timedelta(hours=1), updated_at=NOW)
        built = collect.make_event(done, "resolved", NOW)
        self.assertEqual(built["duration"], "1h 00m")

    def test_event_ids_separate_successive_escalations(self):
        first = collect.make_event(incident(impact="major"), "escalated", NOW)
        second = collect.make_event(incident(impact="critical"), "escalated", NOW)
        self.assertNotEqual(first["id"], second["id"])


if __name__ == "__main__":
    unittest.main()
