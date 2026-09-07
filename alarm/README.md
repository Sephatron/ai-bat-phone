# Alarm clock

A Cloudflare Worker whose only job is to trigger the `poll` workflow on a
schedule. It does no polling and holds no state.

## Why it exists

GitHub's `schedule` trigger works for this repo, but barely. Measured over the
first 63 hours:

| | |
| --- | --- |
| Runs requested by the cron | 126 |
| Runs GitHub actually started | 21 |
| Delivered | 17% |
| Median gap between runs | 3 hours |
| Worst gap | 5h 13m |

GitHub documents this: the schedule event is delayed under high load and "some
queued jobs may be dropped". A five-hour blind spot is too long for a feed whose
whole job is to tell you whether an outage is real.

Manually dispatched runs, by contrast, have never failed. So the fix is not to
move the work, it is to move the timer: Cloudflare's cron fires reliably, and it
presses the button that already works.

The GitHub cron is left in place as a second, unreliable alarm. Duplicate runs
are harmless — the workflow has a concurrency group, and a run that finds
nothing changed writes nothing.

## Setting it up

The Worker is deployed already. It needs one secret: a GitHub token with
permission to start that one workflow.

**1. Create a fine-grained personal access token.**
<https://github.com/settings/personal-access-tokens/new>

- Resource owner: your own account
- Repository access: **Only select repositories** → `Sephatron/ai-bat-phone`
- Repository permissions: **Actions: Read and write** (that alone; nothing else)
- Expiry: your call. Whatever you pick, the alarm stops when it lapses.

**2. Give it to the Worker.** Paste the token when prompted:

```bash
cd alarm && npx wrangler secret put GITHUB_TOKEN
```

**3. Check it worked.**

```bash
cd alarm && npx wrangler tail --format pretty
```

Within fifteen minutes you should see `dispatched poll.yml on main`. A `401` or
`403` there means the token is wrong or lacks the Actions permission.

## When it breaks

It will, eventually, and most likely because the token expired. You do not have
to be watching:

- `lastBuildDate` in all three feeds stops moving.
- The index page renders how long ago the last check was and marks itself
  overdue past two hours.
- The weekly "still watching, nothing to report" item stops arriving in the
  feed, which is the only one of the three signals that reaches a subscriber
  inside their reader.

To find out why, `npx wrangler tail` or the Worker's logs in the Cloudflare
dashboard.
