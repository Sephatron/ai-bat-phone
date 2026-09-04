<?xml version="1.0" encoding="UTF-8"?>
<!-- Browsers apply this when someone opens a feed URL directly; feed readers
     ignore it entirely. Without it, tapping feed.xml on a phone shows a wall
     of raw XML and a non-technical visitor has hit a dead end. -->
<xsl:stylesheet version="1.0" xmlns:xsl="http://www.w3.org/1999/XSL/Transform">
<xsl:output method="html" encoding="UTF-8" indent="yes"/>
<xsl:template match="/rss/channel">
<html lang="en-GB"><head>
<meta charset="utf-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title><xsl:value-of select="title"/></title>
<style>
:root{color-scheme:light dark;--bg:#fbfaf9;--fg:#1c1b1a;--dim:#6b6764;--card:#fff;--line:#e6e2de}
@media (prefers-color-scheme:dark){:root{--bg:#141312;--fg:#eceae8;--dim:#9a948f;--card:#1e1d1b;--line:#2e2c2a}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
.wrap{max-width:720px;margin:0 auto;padding:40px 20px 72px}
h1{font-size:26px;margin:0 0 6px;font-weight:600}
p.tag{color:var(--dim);margin:0 0 20px}
.note{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;font-size:14px;margin:0 0 28px}
.note code{word-break:break-all;user-select:all}
h2{font-size:16px;margin:32px 0 10px;font-weight:600}
ul{list-style:none;padding:0;margin:0}
li{padding:12px 0;border-top:1px solid var(--line)}
a.t{color:inherit;text-decoration:none;font-weight:600;display:block}
a.t:hover{text-decoration:underline}
.m{color:var(--dim);font-size:13px;margin-top:3px}
footer{margin-top:40px;padding-top:14px;border-top:1px solid var(--line);color:var(--dim);font-size:13px}
</style></head><body><div class="wrap">
<h1><xsl:value-of select="title"/></h1>
<p class="tag"><xsl:value-of select="description"/></p>
<p class="note">This is an RSS feed, not a web page. To follow it, paste this address into
any feed reader:<br/><br/><code><xsl:value-of select="atom:link/@href"
xmlns:atom="http://www.w3.org/2005/Atom"/></code><br/><br/>
Last checked <xsl:value-of select="lastBuildDate"/>. A check time that stops moving means
the collector has died rather than that the models are behaving.</p>
<h2>Latest items</h2>
<ul>
<xsl:for-each select="item">
<li>
<a class="t"><xsl:attribute name="href"><xsl:value-of select="link"/></xsl:attribute>
<xsl:value-of select="title"/></a>
<div class="m"><xsl:value-of select="category"/> · <xsl:value-of select="pubDate"/></div>
</li>
</xsl:for-each>
</ul>
<footer><a href="./">All three feeds and the full list of watched providers</a></footer>
</div></body></html>
</xsl:template>
</xsl:stylesheet>
