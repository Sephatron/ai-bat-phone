"""Render the stored event log as RSS 2.0 feeds and a human-readable index.

Everything interpolated here originated on a third party's status page, so text
goes through escape() and anything landing in an HTML attribute goes through
quoteattr(). escape() does not touch quote characters — using it inside an
attribute is an attribute breakout waiting for one hostile status page.
"""

from datetime import datetime, timezone
from email.utils import format_datetime
from string import Template
from xml.sax.saxutils import escape, quoteattr

import adapters

SITE_TITLE = "AI Bat Phone"
SITE_TAGLINE = "When the models go down, you get told to touch grass."
SITE_URL = "https://sephatron.github.io/ai-bat-phone"
SOURCE_URL = "https://github.com/Sephatron/ai-bat-phone"

# Meta events — "we cannot read this provider's status page" — belong in every
# variant including major.xml. A blind spot during a major outage is exactly
# when a major-only subscriber most needs to know the monitor is not looking.
VARIANTS = {
    "feed.xml": {
        "title": SITE_TITLE,
        "description": "Outages and scheduled maintenance across the major AI providers.",
        "filter": lambda e: True,
    },
    "outages.xml": {
        "title": SITE_TITLE + " — outages only",
        "description": "Unplanned incidents only. No scheduled maintenance.",
        "filter": lambda e: e["kind"] != "maintenance",
    },
    "major.xml": {
        "title": SITE_TITLE + " — major only",
        "description": "Major and critical incidents only. The genuinely down-tools ones.",
        "filter": lambda e: e["kind"] == "meta"
        or (e["kind"] == "incident" and e["impact"] in ("major", "critical")),
    },
}

# Providers set impact "none" for informational notices. Showing a reader the
# word "none" next to a headline saying something is broken reads as a bug.
IMPACT_LABEL = {
    "meta": "monitoring",
    "none": "unclassified",
    "minor": "minor",
    "major": "major",
    "critical": "critical",
}


def _url(event):
    """Re-check the URL here rather than trusting events.json.

    That file is a persistence boundary: it outlives the code that wrote it, and
    validating once at capture time makes the invariant depend on every future
    writer remembering. quoteattr stops a breakout; only this stops a
    javascript: scheme.
    """
    return adapters.safe_url(event.get("url", ""), SOURCE_URL)


def _cdata(text):
    # A literal ]]> would close the section early and break every reader.
    return "<![CDATA[%s]]>" % (text or "").replace("]]>", "]]&gt;")


# Feeds carry a stylesheet so that tapping feed.xml on a phone shows a page
# rather than a wall of XML. Readers ignore it.
XSL_HREF = "feed.xsl"


def _rfc822(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return format_datetime(parsed.astimezone(timezone.utc))


def _item_body(event):
    lines = ["<p><strong>%s</strong></p>" % escape(event["title"])]
    facts = []
    if event["kind"] == "maintenance":
        facts.append("Scheduled maintenance")
    elif event["kind"] == "meta":
        facts.append("Monitoring problem at this end, not necessarily at theirs")
    else:
        facts.append("Impact: %s" % IMPACT_LABEL.get(event["impact"], event["impact"]))
    if event["kind"] != "meta":
        facts.append("Status: %s" % event["status"].replace("_", " "))
    if event.get("components"):
        facts.append("Affects: %s" % ", ".join(event["components"]))
    if event.get("duration"):
        facts.append("Duration: %s" % event["duration"])
    if event.get("started_at") and event["kind"] != "meta":
        facts.append("Opened: %s" % _rfc822(event["started_at"]))
    lines.append("<p>%s</p>" % escape(" · ".join(facts)))
    if event.get("body"):
        lines.append("<p>%s</p>" % escape(event["body"]))
    lines.append(
        "<p><a href=%s>%s status page</a></p>"
        % (quoteattr(_url(event)), escape(event["provider_name"]))
    )
    return "".join(lines)


def build_rss(events, filename, heartbeat, max_items=200):
    """Render one feed variant.

    `heartbeat` is the last successful poll rounded to the hour, and it is what
    lastBuildDate carries. That makes the field mean "we looked" rather than "we
    published", so a reader can tell a quiet week from a dead collector, while
    the hour rounding stops it changing on every ten-minute poll.
    """
    spec = VARIANTS[filename]
    selected = [e for e in events if spec["filter"](e)][:max_items]
    self_url = "%s/%s" % (SITE_URL, filename)

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<?xml-stylesheet type="text/xsl" href="%s"?>' % XSL_HREF,
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        "<title>%s</title>" % escape(spec["title"]),
        "<link>%s</link>" % escape(SITE_URL),
        "<description>%s</description>" % escape(spec["description"]),
        "<language>en-GB</language>",
        "<lastBuildDate>%s</lastBuildDate>" % _rfc822(heartbeat),
        "<ttl>30</ttl>",
        "<atom:link href=%s rel='self' type='application/rss+xml'/>" % quoteattr(self_url),
    ]
    for event in selected:
        parts += [
            "<item>",
            "<title>%s</title>" % _cdata(escape(event["headline"])),
            "<link>%s</link>" % escape(_url(event)),
            "<guid isPermaLink='false'>%s</guid>" % escape(event["id"]),
            "<pubDate>%s</pubDate>" % _rfc822(event["published_at"]),
            "<category>%s</category>" % escape(event["provider_name"]),
            "<description>%s</description>" % _cdata(_item_body(event)),
            "</item>",
        ]
    parts += ["</channel>", "</rss>", ""]
    return "\n".join(parts)


BADGE = {
    "critical": ("#7f1d1d", "#fee2e2"),
    "major": ("#7f1d1d", "#fee2e2"),
    "minor": ("#7c2d12", "#ffedd5"),
    "none": ("#334155", "#e2e8f0"),
    "maintenance": ("#1e3a5f", "#dbeafe"),
    "meta": ("#4c1d95", "#ede9fe"),
}

FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http%3A//www.w3.org/2000/svg' viewBox='0 0 100 100'%3E"
    "%3Ctext y='.9em' font-size='90'%3E%F0%9F%93%9E%3C/text%3E%3C/svg%3E"
)

# Kept out of the page template below on purpose: that template is %-formatted,
# and a single literal % in a CSS rule (max-width:100%) would raise TypeError
# and take down the only job this repo runs.
STYLE = """
:root{color-scheme:light dark;--bg:#fbfaf9;--fg:#1c1b1a;--dim:#6b6764;--card:#fff;--line:#e6e2de}
@media (prefers-color-scheme:dark){:root{--bg:#141312;--fg:#eceae8;--dim:#9a948f;--card:#1e1d1b;--line:#2e2c2a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:48px 20px 80px}
h1{font-size:30px;margin:0 0 6px;font-weight:600}
h2{font-size:17px;margin:40px 0 12px;font-weight:600}
p.tag{color:var(--dim);margin:0 0 20px}
.beat{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 14px;margin:0 0 24px;font-size:14px}
.beat b{font-weight:600}
.beat.overdue{border-color:#b45309;background:#fffbeb;color:#7c2d12}
@media (prefers-color-scheme:dark){.beat.overdue{background:#2a1f0a;color:#fcd34d}}
.feeds{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
.feed{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.feed a{font-weight:600;text-decoration:none;color:inherit}
.feed div{color:var(--dim);font-size:13px;margin-top:2px}
.feed code{display:block;margin-top:6px;font-size:11px;color:var(--dim);word-break:break-all;user-select:all}
ul.plain{list-style:none;padding:0;margin:0;columns:2;font-size:14px}
ul.plain li{margin:0 0 4px;break-inside:avoid}
ul.events{list-style:none;padding:0;margin:0}
li.event{display:flex;gap:14px;padding:12px 0;border-top:1px solid var(--line)}
.when{color:var(--dim);font-size:13px;min-width:118px;padding-top:2px}
a.headline{color:inherit;text-decoration:none;font-weight:600}
a.headline:hover{text-decoration:underline}
.emoji{display:inline-block;margin-right:7px;font-style:normal}
.meta{display:flex;gap:8px;flex-wrap:wrap;align-items:center;color:var(--dim);font-size:13px;margin-top:3px}
.badge{font-size:11px;text-transform:uppercase;letter-spacing:.04em;padding:2px 7px;border-radius:99px;font-weight:600}
.dim{color:var(--dim)}
footer{margin-top:48px;padding-top:16px;border-top:1px solid var(--line);color:var(--dim);font-size:13px}
@media (max-width:460px){li.event{display:block}.when{min-width:0;margin-bottom:2px}ul.plain{columns:1}}
"""


def _split_badge(headline):
    """Separate a leading emoji from the words, so the page can space them.

    Emoji glyphs sit tight against following text at these sizes; RSS readers
    do their own thing, so the split is presentation-only and the stored
    headline is untouched.
    """
    head, _, rest = headline.partition(" ")
    if rest and head and not head.isascii():
        return head, rest
    return "", headline


# How far behind the last poll may fall before the page calls itself stale.
# GitHub's scheduler is best-effort and drifts, so this is deliberately loose.
OVERDUE_MINUTES = 120

# string.Template, not %-formatting. The previous version was one %-format
# string, so a literal % anywhere in the page copy — "100% of the roster", a
# CSS width, a URL-encoded character — raised TypeError and killed the only job
# this repo runs. Moving the CSS out narrowed that hazard; it did not close it.
PAGE = Template("""<!doctype html>
<html lang="en-GB"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>$title</title>
<meta name="description" content="$tagline">
<link rel="icon" href="$favicon">
<link rel="alternate" type="application/rss+xml" title="$title" href="feed.xml">
<style>$style</style></head><body><div class="wrap">
<h1>$title</h1>
<p class="tag">$tagline</p>

<p class="beat" id="beat">Status pages last checked
<b><time datetime="$beat_iso" id="beat-time">$beat_human</time></b><span id="beat-age"></span>.
Every feed carries this time even when nothing has happened, so a check time that stops
moving means the collector has died rather than that the models are behaving.</p>

<div class="feeds">
<div class="feed"><a href="feed.xml">feed.xml</a><div>Everything — outages and maintenance</div><code>$site/feed.xml</code></div>
<div class="feed"><a href="outages.xml">outages.xml</a><div>Outages only, no planned works</div><code>$site/outages.xml</code></div>
<div class="feed"><a href="major.xml">major.xml</a><div>Major and critical only</div><code>$site/major.xml</code></div>
</div>
<p class="dim">Paste one of those URLs into any feed reader. RSS has no per-subscriber
settings, so the maintenance toggle is a choice of URL. Only providers on the statuspage
and rss adapters publish maintenance at all; for the rest, feed.xml and outages.xml carry
the same items.</p>

<h2>Recent</h2>
<ul class="events">$rows</ul>

<h2>Watching</h2>
<ul class="plain">$watched</ul>
$blocks

<footer>Checked twice an hour by a GitHub Action, best-effort ·
<a href="$source">source</a></footer>
</div>
<script>
// The page is only rebuilt when something changes, so a build-time "x minutes
// ago" would be frozen and wrong. Compute it in the reader's browser instead.
(function () {
  var el = document.getElementById("beat-time");
  var out = document.getElementById("beat-age");
  if (!el || !out) return;
  var when = new Date(el.getAttribute("datetime"));
  if (isNaN(when)) return;
  var mins = Math.floor((Date.now() - when.getTime()) / 60000);
  if (mins < 0) mins = 0;
  var text = mins < 60 ? mins + " min ago"
    : Math.floor(mins / 60) + "h " + (mins % 60) + "m ago";
  out.textContent = " (" + text + ")";
  if (mins > $overdue_minutes) {
    document.getElementById("beat").className = "beat overdue";
    out.textContent = " (" + text + " — overdue, this feed may be stale)";
  }
})();
</script>
</body></html>
""")


def build_index(events, providers, unreachable, heartbeat, gaps=None, max_items=60):
    rows = []
    for event in events[:max_items]:
        if event["kind"] == "meta":
            band = "meta"
        elif event["kind"] == "maintenance":
            band = "maintenance"
        else:
            band = event["impact"]
        fg, bg = BADGE.get(band, BADGE["none"])
        when = datetime.fromisoformat(event["published_at"].replace("Z", "+00:00"))
        emoji, words = _split_badge(event["headline"])
        rows.append(
            '<li class="event">'
            '<div class="when">%s</div>'
            '<div><a class="headline" href=%s><span class="emoji">%s</span>%s</a>'
            '<div class="meta"><span class="badge" style="color:%s;background:%s">%s</span>'
            "<span>%s</span><span>%s</span></div></div></li>"
            % (
                when.strftime("%d %b %H:%M UTC"),
                quoteattr(_url(event)),
                escape(emoji),
                escape(words),
                fg,
                bg,
                escape(IMPACT_LABEL.get(band, band)),
                escape(event["provider_name"]),
                escape(event["title"]),
            )
        )

    watched = "".join(
        '<li>%s <span class="dim">%s%s</span></li>'
        % (
            escape(p["name"]),
            escape(p["adapter"]),
            "" if p["adapter"] in adapters.MAINTENANCE_CAPABLE else ", incidents only",
        )
        for p in providers
    )

    blocks = []
    if unreachable:
        rows_out = "".join(
            "<li>%s <span class=\"dim\">%s</span></li>" % (escape(k), escape(v))
            for k, v in sorted(unreachable.items())
        )
        blocks.append(
            "<h2>Not readable right now</h2><ul class=\"plain\">%s</ul>"
            "<p class=\"dim\">These have failed several polls in a row. This feed is blind to them, "
            "so its silence about them means nothing either way. Each one also appears as an item in "
            "the feeds.</p>" % rows_out
        )
    if gaps:
        rows_out = "".join(
            "<li>%s <span class=\"dim\">%s</span></li>" % (escape(g["name"]), escape(g.get("note", "")))
            for g in gaps
        )
        blocks.append(
            "<h2>Not watched</h2><ul class=\"plain\">%s</ul>"
            "<p class=\"dim\">An outage at any of these will never appear here. The reason is next "
            "to each one.</p>" % rows_out
        )

    return PAGE.substitute(
        title=escape(SITE_TITLE),
        tagline=escape(SITE_TAGLINE),
        favicon=FAVICON,
        style=STYLE,
        site=escape(SITE_URL),
        source=escape(SOURCE_URL),
        beat_iso=heartbeat.isoformat(),
        beat_human=heartbeat.strftime("%d %b %Y %H:00 UTC"),
        overdue_minutes=OVERDUE_MINUTES,
        rows="".join(rows)
        or '<li class="event"><div class="when">—</div><div class="dim">Nothing yet. Enjoy it.</div></li>',
        watched=watched,
        blocks="".join(blocks),
    )
