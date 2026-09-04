# AI Bat Phone

An RSS feed that tells you when the AI models have fallen over, so you can stop
debugging your own code and go and touch some grass.

**Feeds**

| URL | What's in it |
| --- | --- |
| [`feed.xml`](https://sephatron.github.io/ai-bat-phone/feed.xml) | Everything — outages and scheduled maintenance |
| [`outages.xml`](https://sephatron.github.io/ai-bat-phone/outages.xml) | Unplanned incidents only |
| [`major.xml`](https://sephatron.github.io/ai-bat-phone/major.xml) | Major and critical only |

RSS has no per-subscriber settings, so the maintenance toggle is a choice of
URL. Subscribe to the one you want.

Human-readable mirror: <https://sephatron.github.io/ai-bat-phone/>

## How it works

A GitHub Action runs `collect.py` every ten minutes. It reads each provider's
status page, compares what it finds against `state.json`, and appends any real
change to `events.json`. The feeds under `docs/` are rebuilt from that log and
served by GitHub Pages.

```
providers.toml ──> adapters.py ──> collect.py ──> events.json ──> feedgen.py ──> docs/*.xml
                                       ^ |
                                  state.json
```

An item is published when an incident is first seen, when its impact escalates,
when its status advances, and when it resolves. Roughly three or four items per
outage, not one every ten minutes.

### Two rules worth knowing

**A provider we cannot read produces nothing.** A timeout, a 503 or a bot
challenge is logged and skipped. It is never turned into a "resolved" item.
Silence from a status page is not recovery, and getting this wrong would break
the feed during exactly the incident it exists for.

**Nothing is published on a quiet run.** Generated files are compared with their
build timestamps masked out, so an unchanged feed is not rewritten and the
Action commits nothing.

## Copy

Jokes go in the title, facts go in the body. Selection is deterministic — the
same incident always gets the same line, so a rebuild never reshuffles headlines
under subscribers. Anything mentioning a security incident, a breach or data
loss drops to neutral copy; see `SOBER` in `copywriter.py`.

## Providers

Fifteen are polled. Three adapters cover them:

- **statuspage** — `/api/v2/incidents.json`. Atlassian Statuspage, Instatus and
  incident.io all serve the same shape. Claude, OpenAI, Cursor, GitHub,
  Windsurf, Vercel, Groq, Cohere, Fireworks, ElevenLabs, Lovable.
- **rss** — `/history.rss`, for pages that block JSON. DeepSeek, Replit,
  Perplexity.
- **gcp** — `status.cloud.google.com/incidents.json`, filtered to Vertex AI and
  Gemini.

### Known gaps

Listed in `providers.toml` with `enabled = false` so the research isn't redone:

- **xAI** — Cloudflare bot challenge on every path, `history.rss` included.
- **Mistral, Hugging Face, Together AI** — JavaScript-only status pages with no
  machine-readable endpoint on their custom domain.

Beating a bot challenge from a CI runner is a maintenance treadmill. If any of
these ever ship a real endpoint, flip `enabled` and it works.

## Local use

```bash
python3 collect.py --dry-run   # report what would change, write nothing
python3 collect.py             # poll and rebuild docs/
python3 -m unittest -v         # 27 offline tests, no network
```

Python 3.11+ (needs `tomllib`). No third-party dependencies, by design — an
unattended job that runs every ten minutes for years should not have a
dependency tree that can rot underneath it.

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

If that is JSON, use `statuspage`. If not, try `/history.rss` and use `rss`.
