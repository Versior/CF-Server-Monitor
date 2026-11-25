const REMOTE_VERSION_URL = 'https://raw.githubusercontent.com/huilang-me/CF-Server-Monitor/refs/heads/main/version.json';
const AGENT_RELEASE_URL = 'https://api.github.com/repos/huilang-me/cfsm-agent/releases/latest';
const REMOTE_VERSION_TTL = 10 * 60 * 1000;
const REMOTE_VERSION_FAILURE_TTL = 30 * 1000;
const REMOTE_VERSION_FETCH_TIMEOUT_MS = 2000;

let cachedRemoteVersion = null;
let cachedRemoteVersionAt = 0;
let cachedRemoteVersionFailureAt = 0;
let refreshing = false;

// Version hints are optional; serve cached data while the owning request refreshes it.
export function getRemoteVersion(ctx) {
  const now = Date.now();
  if (!refreshing &&
      (!cachedRemoteVersion || now - cachedRemoteVersionAt >= REMOTE_VERSION_TTL) &&
      (!cachedRemoteVersionFailureAt || now - cachedRemoteVersionFailureAt >= REMOTE_VERSION_FAILURE_TTL)) {
    refreshing = true;
    ctx.waitUntil(fetchRemoteVersion().finally(() => { refreshing = false; }));
  }
  return cachedRemoteVersion;
}

async function fetchRemoteVersion() {
  const [version, release] = await Promise.allSettled([
    fetchJsonWithTimeout(REMOTE_VERSION_URL, { Accept: 'application/json' }),
    fetchJsonWithTimeout(AGENT_RELEASE_URL, { Accept: 'application/vnd.github+json', 'User-Agent': 'CF-Server-Monitor' })
  ]);

  const workers = version.status === 'fulfilled' && typeof version.value?.workers === 'string'
    ? version.value.workers.trim() : '';
  const agent = release.status === 'fulfilled' && typeof release.value?.tag_name === 'string'
    ? release.value.tag_name.trim() : '';
  if (!workers && !agent) {
    cachedRemoteVersionFailureAt = Date.now();
    return;
  }

  cachedRemoteVersion = {
    workers: workers || cachedRemoteVersion?.workers || null,
    agent: agent || cachedRemoteVersion?.agent || null
  };
  cachedRemoteVersionAt = Date.now();
  cachedRemoteVersionFailureAt = 0;
}

async function fetchJsonWithTimeout(url, headers) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), REMOTE_VERSION_FETCH_TIMEOUT_MS);
  try {
    const response = await fetch(url, { headers, signal: controller.signal });
    if (!response.ok) {
      await response.body?.cancel();
      return null;
    }
    return await response.json();
  } finally {
    clearTimeout(timeout);
  }
}
