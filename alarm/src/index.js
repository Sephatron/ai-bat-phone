/**
 * AI Bat Phone — alarm clock.
 *
 * The collector lives in GitHub Actions and works. Its scheduler does not: the
 * `schedule` trigger on this repo fired zero times in six hours across two cron
 * expressions, while all four manually dispatched runs succeeded. GitHub
 * documents the event as best-effort and says queued jobs "may be dropped".
 *
 * So this Worker is the timer, and nothing else. It presses the button that
 * already works. Everything downstream — the Python, the tests, the feed URLs —
 * is untouched.
 *
 * If this Worker stops, the feed says so on its own: `lastBuildDate` stops
 * moving, the index page marks itself overdue, and the weekly "still watching"
 * item stops arriving. That is the safety net, and it is why this file is
 * allowed to be thirty lines with no health endpoint of its own.
 */

const OWNER = "Sephatron";
const REPO = "ai-bat-phone";
const WORKFLOW = "poll.yml";
const REF = "main";

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatch(env));
  },

  // Only reachable via `wrangler dev`; workers_dev is off, so there is no
  // public route. It exists so the Worker can be exercised locally.
  async fetch(request, env) {
    return new Response(
      "AI Bat Phone alarm clock. Triggers the poll workflow on a schedule. Takes no requests.\n",
      { headers: { "content-type": "text/plain; charset=utf-8" } },
    );
  },
};

async function dispatch(env) {
  if (!env.GITHUB_TOKEN) {
    console.error("GITHUB_TOKEN is not set. Run: npx wrangler secret put GITHUB_TOKEN");
    return;
  }

  const url = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`;
  let response;
  try {
    response = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GITHUB_TOKEN}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        // GitHub rejects API requests with no User-Agent.
        "User-Agent": "ai-bat-phone-alarm",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ ref: REF }),
    });
  } catch (error) {
    console.error(`could not reach GitHub: ${error}`);
    return;
  }

  if (response.status === 204) {
    console.log(`dispatched ${WORKFLOW} on ${REF}`);
    return;
  }

  // 401/403 means the token has expired or lost its Actions permission, which
  // is the most likely way this quietly stops working a year from now.
  const body = await response.text();
  console.error(`dispatch failed: HTTP ${response.status} ${body.slice(0, 300)}`);
}
