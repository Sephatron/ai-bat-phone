"""Render the stored event log as RSS 2.0 feeds and a human-readable index."""

from datetime import datetime, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

SITE_TITLE = "AI Bat Phone"
SITE_TAGLINE = "When the models go down, you get told to touch grass."

VARIANTS = {
    "feed.xml": {
        "title": SITE_TITLE,
        "description": "Outages and scheduled maintenance across the major AI providers.",
        "filter": lambda e: True,
    },
    "outages.xml": {
        "title": SITE_TITLE + " — outages only",
        "description": "Unplanned incidents only. No scheduled maintenance.",
        "filter": lambda e: e["kind"] == "incident",
    },
    "major.xml": {
        "title": SITE_TITLE + " — major only",
        "description": "Major and critical incidents only. The genuinely down-tools ones.",
        "filter": lambda e: e["kind"] == "incident" and e["impact"] in ("major", "critical"),
    },
}


def _cdata(text):
    # A literal ]]> would close the section early and break every reader.
    return "<![CDATA[%s]]>" % (text or "").replace("]]>", "]]&gt;")


def _rfc822(iso):
    parsed = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return format_datetime(parsed.astimezone(timezone.utc))


def _item_body(event):
    lines = ["<p><strong>%s</strong></p>" % escape(event["title"])]
    facts = []
    if event["kind"] == "maintenance":
        facts.append("Scheduled maintenance")
    else:
        facts.append("Impact: %s" % event["impact"])
    facts.append("Status: %s" % event["status"].replace("_", " "))
    if event.get("components"):
        facts.append("Affects: %s" % ", ".join(event["components"]))
    if event.get("duration"):
        facts.append("Duration: %s" % event["duration"])
    lines.append("<p>%s</p>" % escape(" · ".join(facts)))
    if event.get("body"):
        lines.append("<p>%s</p>" % escape(event["body"]))
    lines.append(
        '<p><a href="%s">%s status page</a></p>' % (escape(event["url"]), escape(event["provider_name"]))
    )
    return "".join(lines)


def build_rss(events, filename, site_url, max_items=200):
    spec = VARIANTS[filename]
    selected = [e for e in events if spec["filter"](e)][:max_items]
    now = format_datetime(datetime.now(timezone.utc))
    self_url = "%s/%s" % (site_url.rstrip("/"), filename)

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        "<title>%s</title>" % escape(spec["title"]),
        "<link>%s</link>" % escape(site_url),
        "<description>%s</description>" % escape(spec["description"]),
        "<language>en-GB</language>",
        "<lastBuildDate>%s</lastBuildDate>" % now,
        "<ttl>10</ttl>",
        '<atom:link href="%s" rel="self" type="application/rss+xml"/>' % escape(self_url),
    ]
    for event in selected:
        parts += [
            "<item>",
            "<title>%s</title>" % _cdata(event["headline"]),
            "<link>%s</link>" % escape(event["url"]),
            '<guid isPermaLink="false">%s</guid>' % escape(event["id"]),
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
}


def build_index(events, providers, failures, site_url, max_items=60):
    now = datetime.now(timezone.utc)
    rows = []
    for event in events[:max_items]:
        band = "maintenance" if event["kind"] == "maintenance" else event["impact"]
        fg, bg = BADGE.get(band, BADGE["none"])
        when = datetime.fromisoformat(event["published_at"].replace("Z", "+00:00"))
        rows.append(
            '<li class="event">'
            '<div class="when">%s</div>'
            '<div><a class="headline" href="%s">%s</a>'
            '<div class="meta"><span class="badge" style="color:%s;background:%s">%s</span>'
            "<span>%s</span><span>%s</span></div></div></li>"
            % (
                when.strftime("%d %b %H:%M UTC"),
                escape(event["url"]),
                escape(event["headline"]),
                fg,
                bg,
                escape(band),
                escape(event["provider_name"]),
                escape(event["title"]),
            )
        )

    watched = "".join(
        '<li>%s <span class="dim">%s</span></li>' % (escape(p["name"]), escape(p["adapter"]))
        for p in providers
    )
    skipped = "".join(
        '<li>%s <span class="dim">%s</span></li>' % (escape(k), escape(v))
        for k, v in sorted(failures.items())
    )
    failures_block = (
        '<h2>Unreachable this run</h2><ul class="plain">%s</ul>'
        '<p class="dim">A provider we cannot read is skipped, never reported as recovered.</p>' % skipped
        if failures
        else ""
    )

    return """<!doctype html>
<html lang="en-GB"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s</title>
<meta name="description" content="%(tagline)s">
<link rel="alternate" type="application/rss+xml" title="%(title)s" href="feed.xml">
<style>
:root{color-scheme:light dark;--bg:#fbfaf9;--fg:#1c1b1a;--dim:#6b6764;--card:#fff;--line:#e6e2de}
@media (prefers-color-scheme:dark){:root{--bg:#141312;--fg:#eceae8;--dim:#9a948f;--card:#1e1d1b;--line:#2e2c2a}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.wrap{max-width:760px;margin:0 auto;padding:48px 20px 80px}
h1{font-size:30px;margin:0 0 6px;font-weight:600}
h2{font-size:17px;margin:40px 0 12px;font-weight:600}
p.tag{color:var(--dim);margin:0 0 28px}
.feeds{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
.feed{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.feed a{font-weight:600;text-decoration:none;color:inherit}
.feed div{color:var(--dim);font-size:13px;margin-top:2px}
ul.plain{list-style:none;padding:0;margin:0;columns:2;font-size:14px}
ul.plain li{margin:0 0 4px;break-inside:avoid}
ul.events{list-style:none;padding:0;margin:0}
li.event{display:flex;gap:14px;padding:12px 0;border-top:1px solid var(--line)}
.when{color:var(--dim);font-size:13px;min-width:118px;padding-top:2px}
a.headline{color:inherit;text-decoration:none;font-weight:600}
a.headline:hover{text-decoration:underline}
.meta{display:flex;gap:8px;flex-wrap:wrap;align-items:center;color:var(--dim);font-size:13px;margin-top:3px}
.badge{font-size:11px;text-transform:uppercase;letter-spacing:.04em;padding:2px 7px;border-radius:99px;font-weight:600}
.dim{color:var(--dim)}
footer{margin-top:48px;padding-top:16px;border-top:1px solid var(--line);color:var(--dim);font-size:13px}
</style></head><body><div class="wrap">
<h1>%(title)s</h1>
<p class="tag">%(tagline)s</p>

<div class="feeds">
<div class="feed"><a href="feed.xml">feed.xml</a><div>Everything — outages and maintenance</div></div>
<div class="feed"><a href="outages.xml">outages.xml</a><div>Outages only, no planned works</div></div>
<div class="feed"><a href="major.xml">major.xml</a><div>Major and critical only</div></div>
</div>

<h2>Recent</h2>
<ul class="events">%(rows)s</ul>

<h2>Watching</h2>
<ul class="plain">%(watched)s</ul>
%(failures)s

<footer>Updated %(now)s · polled every 10 minutes by a GitHub Action ·
<a href="https://github.com/Sephatron/ai-bat-phone">source</a></footer>
</div></body></html>
""" % {
        "title": escape(SITE_TITLE),
        "tagline": escape(SITE_TAGLINE),
        "rows": "".join(rows) or '<li class="event"><div class="when">—</div><div class="dim">Nothing yet. Enjoy it.</div></li>',
        "watched": watched,
        "failures": failures_block,
        "now": now.strftime("%d %b %Y %H:%M UTC"),
    }
