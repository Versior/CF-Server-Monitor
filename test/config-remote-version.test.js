import test from 'node:test';
import assert from 'node:assert/strict';
import { Miniflare } from 'miniflare';
import worker from '../src/index.js';
import { generateToken } from '../src/middleware/auth.js';
import { saveSiteOptions } from '../src/utils/settings.js';

test('site config returns before GitHub finishes and exposes cached versions only to logged-in clients', { timeout: 5000 }, async (t) => {
  const miniflare = new Miniflare({
    modules: true,
    script: 'export default { fetch() { return new Response("OK"); } };',
    d1Databases: { DB: 'config-remote-version-test' }
  });
  t.after(() => miniflare.dispose());
  const env = { DB: await miniflare.getD1Database('DB'), API_SECRET: 'local-test-only' };
  const tasks = [];
  const ctx = { waitUntil: task => tasks.push(task) };
  const config = async (token) => {
    const request = new Request('https://monitor.example.invalid/api/config', {
      headers: token ? { Authorization: `Bearer ${token}` } : {}
    });
    const response = await worker.fetch(request, env, ctx);
    assert.equal(response.status, 200);
    return response.json();
  };

  let finishUpstream;
  const upstream = new Promise(resolve => { finishUpstream = resolve; });
  const requests = [];
  t.mock.method(globalThis, 'fetch', async (url) => {
    const host = new URL(url).hostname;
    requests.push(host);
    await upstream;
    return Response.json(host === 'raw.githubusercontent.com' ? { workers: '9.0.0' } : { tag_name: 'v8.0.0' });
  });

  try {
    const guest = await config();
    assert.equal(guest.authorization, false);
    assert.equal('last_workers_version' in guest, false);
    assert.deepEqual(requests, []);

    const sys = await saveSiteOptions(env.DB, { jwt_secret: 'local-config-test-session-signing-key' });
    const token = await generateToken(env, sys);
    // Bound the assertion independently of the unresolved upstream responses.
    const first = await Promise.race([config(token), new Promise((_, reject) => {
      setTimeout(() => reject(new Error('site config waited for GitHub')), 750).unref();
    })]);
    assert.equal(first.authorization, true);
    assert.equal(first.last_workers_version, null);
    assert.equal(first.last_agent_version, null);
    assert.equal((await config(token)).last_workers_version, null);
    assert.deepEqual(requests, ['raw.githubusercontent.com', 'api.github.com']);
    assert.equal(tasks.length, 1);

    finishUpstream();
    await Promise.all(tasks);
    const cached = await config(token);
    assert.equal(cached.last_workers_version, '9.0.0');
    assert.equal(cached.last_agent_version, 'v8.0.0');
    assert.equal('last_agent_version' in await config(), false);
    assert.equal(requests.length, 2);
  } finally {
    finishUpstream();
    await Promise.all(tasks);
  }
});
