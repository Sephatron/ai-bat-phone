# AI Bat Phone

An RSS feed that tells you when the AI models have fallen over, so you can stop
debugging your own code and go and touch some grass.

**Feeds**

| URL | What's in it |
| --- | --- |
| [`feed.xml`](https://sephatron.github.io/ai-bat-phone/feed.xml) | Everything — outages and scheduled maintenance |
| [`outages.xml`](https://sephatron.github.io/ai-bat-phone/outages.xml) | Unplanned incidents only |
| [`major.xml`](https://sephatron.github.io/ai-bat-phone/major.xml) | Major and critical only |

I CBA making per-subscriber settings (soz) so you've got three tiered urls instead.
Subscribe to the one you want. All three also carry monitoring failures -
see "When we go blind" below.

Human-readable mirror: <https://sephatron.github.io/ai-bat-phone/>

## How it works

A GitHub Action runs `collect.py`, triggered every 15 minutes by a small
Cloudflare Worker in [`alarm/`](alarm/). I started this thinking Github's `schedule` feature would carry it but that thing failed SO quick - it's there as a lame backup but the real engine is CF.
It reads each provider's status page, compares what it finds against `state.json`, and appends any real
change to `events.json`. The feeds under `docs/` are rebuilt from that log and
served by GitHub Pages.

```
providers.toml ──> adapters.py ──> collect.py ──> events.json ──> feedgen.py ──> docs/*.xml
                                       ^ |
                                  state.json
```

An item is published when an incident is first seen, when its impact escalates,
when its status advances, when it is reopened, and when it resolves. Roughly
three or four items per outage.

## Four rules worth knowing

**A provider we cannot read produces no incident events.** A timeout, a 503 or a
bot challenge is skipped. Silence from a status page is not treated as recovery - duh.

**When we go blind, we say so.** After three consecutive failed polls a provider
gets its own item, in all three feeds: *"Cannot reach X's status page. Treat this
feed's silence about X as unknown, not good."* A recovery item follows when it
comes back. This is the counterweight to the rule above. 

**Three separate proofs that the collector is alive.** No mainstream reader
shows `lastBuildDate` to a human, so on its own it proves liveness to nobody who
is not running curl. So: `lastBuildDate` carries the last successful poll rounded
to the hour, for anyone reading the XML; the index page renders that time and how
long ago it was, and marks itself overdue past two hours; and a "still watching,
nothing to report" item goes into all three feeds weekly, which is the only one
of the three that reaches a subscriber inside their reader.

**Items are stamped with when we noticed, not when the incident began.** This is
an alert stream, not an archive. An all-clear backdated three days sorts below
everything already read and is never seen. The provider's own start time is in
the item body.

## Copy

Jokes go in the title, facts go in the body. Selection is deterministic — the
same incident always gets the same line, so a rebuild never reshuffles headlines
under subscribers. Anything mentioning security, a breach, credentials, data
loss or corruption drops to neutral copy, as do all monitoring-failure items;
see `SOBER` in `copywriter.py`.

## Providers

Twenty-one are polled. Three adapters cover them:

- **statuspage** — `/api/v2/incidents.json`. Atlassian Statuspage, Instatus and
  incident.io all serve the same shape. Claude, OpenAI, Cursor, GitHub,
  Windsurf, Vercel, Groq, Cohere, Fireworks, ElevenLabs, Lovable, Cerebras,
  SambaNova, Baseten, AI21, Moonshot AI (Kimi).
- **rss** — a history feed, for pages with no usable JSON API. DeepSeek,
  Replit, Perplexity, OpenRouter. The feed path is configurable per provider
  (`path`), because it is not always `/history.rss`. Four dialects are
  recognised; an entry in none of them is skipped and counted rather than
  guessed at, and a feed where none parse raises.
- **gcp** — `status.cloud.google.com/incidents.json`, filtered to Vertex AI and
  Gemini.

### Maintenance coverage is partial, deliberately

Only Atlassian Statuspage serves `/api/v2/scheduled-maintenances.json`. Instatus
and incident.io return 404, and the `gcp` adapter reports incidents only. So for
roughly two thirds of the roster, `feed.xml` and `outages.xml` carry identical
items and always will. The index page says so too.

### Not watched

Listed in `providers.toml` with `enabled = false` and a reason each, and
published on the index page so a reader knows the difference between "no
outages" and "not looking":

- **xAI** — Cloudflare bot challenge on every path, history feed included.
- **Mistral, Hugging Face, Together AI** — JavaScript-only status pages with no
  machine-readable endpoint on their custom domain.
- **AWS Bedrock** — the RSS feed is one item per *update* rather than per
  incident, and covers all of AWS. Needs its own adapter.
- **Azure OpenAI** — the status feed returns zero items.
- **Stability, Deepgram, AssemblyAI** — reachable, but images and speech rather
  than language models. Flip `enabled` to include them.

Blocks marked `blocked = true` cannot be read at all, so their `adapter` and
`base` are guesses rather than verified config; flipping `enabled` on one of
those gives you a broken provider, not a working one. The three out-of-scope
ones at the end are reachable and work as they stand.

## Local use

```bash
python3 collect.py --dry-run   # report what would change, write nothing
python3 collect.py             # poll and rebuild docs/
python3 -m unittest -v         # 90 offline tests, no network
```

Python 3.11+ (needs `tomllib`). No third-party dependencies, by design — an
unattended job that runs every ten minutes for years should not have a
dependency tree that can rot underneath it. XML parsing leans on CPython's
expat, which since 2.4 blocks entity expansion and caps input amplification;
that is the protection, and there is no `defusedxml` behind it.

Everything the collector reads comes from third parties, so `adapters.py` is the
trust boundary: strings are stripped of characters illegal in XML and bounded in
length, URLs are restricted to well-formed `http(s)`, and responses are capped at
8MB decompressed. `feedgen.py` uses `quoteattr()` for anything landing in an HTML
attribute — `escape()` does not touch quote characters, which is an attribute
breakout waiting for one hostile status page — and re-checks every URL at render
time rather than trusting what `events.json` recorded, because that file outlives
the code that wrote it.

An adapter that stops recognising a provider's format raises rather than
returning an empty list. A quiet provider and a broken parser must not look the
same, and returning `[]` would reset the failure counter and suppress the alarm.

## Adding a provider

Append a block to `providers.toml`:

```toml
[[provider]]
key = "someone"
name = "Someone AI"
adapter = "statuspage"
base = "https://status.someone.ai"
```

Check it first — most status hosts answer `200` with an HTML shell for unknown
paths, so a `200` alone proves nothing:

```bash
curl -s https://status.someone.ai/api/v2/incidents.json | head -c 200
```

If that is JSON, use `statuspage`. If not, try `/history.rss` and use `rss`,
adding a `path` key if the feed lives somewhere else — OpenRouter's is at
`/incidents.rss`. A `path` must start with `/`, which is checked at load: a
value like `@evil.test/x` appended to a base would send the request to a
different host entirely.

Check the links too. A feed whose `<link>` elements have no scheme is common
enough that the adapter resolves them, but a feed doing something stranger will
publish items pointing at the wrong place.
