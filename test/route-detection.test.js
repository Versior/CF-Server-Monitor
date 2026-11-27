import test, { after } from 'node:test';
import assert from 'node:assert/strict';
import { Miniflare } from 'miniflare';
import { createServer } from 'vite';
import vue from '@vitejs/plugin-vue';
import { createSSRApp, h } from 'vue';
import { renderToString } from 'vue/server-renderer';
import { initDatabase } from '../src/database/schema.js';
import { addServerColumns } from '../src/database/updateDatabase.js';
import { handleRouteDetection } from '../src/handlers/routeDetection.js';
import { handleServerAPI, handleServersAPI } from '../src/handlers/dashboard.js';
import { clearAllCaches, getAllServers } from '../src/utils/cache.js';
import { normalizeRouteReport } from '../src/utils/routeDetection.js';
import { automaticRouteTags } from '../src/frontend/utils/routeDetection.js';
import { LANGUAGES } from '../src/frontend/locales/index.js';

const evidence = (overrides = {}) => ({ carrier: 'ct', family: 4, status: 'ok', completed: true, hops: [[64500], [], [4809], [4134]], ...overrides });
const report = (routes = [evidence()]) => ({ id: 'routes-demo', routes });

test('route cards and the evidence panel use configured carrier names or the current locale', async (context) => {
  const storage = Object.getOwnPropertyDescriptor(globalThis, 'localStorage');
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: { getItem: () => null } });
  context.after(() => { if (storage) Object.defineProperty(globalThis, 'localStorage', storage); else delete globalThis.localStorage; });
  const vite = await createServer({ configFile: false, plugins: [vue()], appType: 'custom', logLevel: 'silent', server: { middlewareMode: true, hmr: false, watch: null } });
  context.after(() => vite.close());
  const [{ default: Panel }, { useServerCardData }, { currentLang, translations }] = await Promise.all([
    '/src/frontend/components/RouteDetectionPanel.vue', '/src/frontend/composables/useServerCardData.js', '/src/frontend/utils/i18n.js'
  ].map(path => vite.ssrLoadModule(path)));
  const routes = [evidence({ hops: [[4809]] }), evidence({ carrier: 'cu', family: 6, hops: [[9929]] }), evidence({ carrier: 'cm', hops: [[58453]] })];
  for (const language of LANGUAGES) {
    const { default: dictionary } = await language.load();
    translations[language.code] = dictionary;
    currentLang.value = language.code;
    const result = normalizeRouteReport(report(routes)).report;
    result.expires_at = Date.now() + 1000;
    for (const sysConfig of [{}, { custom_ct_name: 'My Telecom', custom_cu_name: 'My Unicom', custom_cm_name: 'My Mobile' }]) {
      const server = { route_report: result };
      const html = await renderToString(createSSRApp({ setup() {
        const { tagList } = useServerCardData({ server, sysConfig });
        return () => h('article', [h('aside', tagList.value.join('|')), h(Panel, { report: result, config: sysConfig })]);
      } }));
      const names = [sysConfig.custom_ct_name || dictionary.customCt, sysConfig.custom_cu_name || dictionary.customCu, sysConfig.custom_cm_name || dictionary.customCm];
      assert.ok(html.includes(`<aside>${names[0]} CN2|${names[1]} 9929|${names[2]} CMI</aside>`), language.code);
      for (const [index, carrier] of names.entries()) assert.ok(html.includes(`${carrier} · IPv${routes[index].family}`), language.code);
    }
  }
});

test('expanded networks distinguish CTGNET, CUG and CMIN2 from CMI', () => {
  const result = normalizeRouteReport(report([evidence({ hops: [[23764], [10099], [58807], [58453]] })])).report;
  assert.deepEqual(result.routes[0].networks, ['CTGNET', 'CUG', 'CMIN2', 'CMI']);
});

test('router prefix hints keep their source separate from observed ASNs', () => {
  const result = normalizeRouteReport(report([evidence({ hops: [[], [4134]],
    prefix_hints: [{ hop: 0, prefix: '59.43.0.0/16' }], protocol: 'icmp', attempts: 2 })])).report.routes[0];
  assert.deepEqual(result.networks, ['163', 'CN2']);
  assert.deepEqual(result.hops, [[], [4134]]);
  assert.deepEqual(result.prefix_hints, [{ hop: 0, prefix: '59.43.0.0/16', network: 'CN2' }]);
  assert.equal(result.protocol, 'icmp');
  assert.equal(result.attempts, 2);
  for (const prefix_hints of [[{ hop: 0, prefix: '0.0.0.0/0' }], [{ hop: 2, prefix: '59.43.0.0/16' }]]) {
    assert.throws(() => normalizeRouteReport(report([evidence({ hops: [[]], prefix_hints })])));
  }
  assert.throws(() => normalizeRouteReport(report([evidence({ status: 'failed', completed: false, hops: [],
    prefix_hints: [{ hop: 0, prefix: '59.43.0.0/16' }] })])));
});

test('international transit is informative without certifying a China backbone', () => {
  const result = normalizeRouteReport(report([evidence({ hops: [[6453], [136958]] })]), 1000).report;
  assert.equal(result.routes[0].status, 'unknown');
  assert.deepEqual(result.routes[0].networks, []);
  assert.deepEqual(result.routes[0].transit_networks, ['TATA']);
  assert.deepEqual(automaticRouteTags(result, { ct: 'CT' }, 1001), ['CT via TATA']);
});

test('ASN evidence identifies mixed backbone networks without asserting GIA or GT', () => {
  const result = normalizeRouteReport({ ...report(), secret: 'FAKE_TEST_VALUE', ip: '203.0.113.8' }, 1000);
  assert.deepEqual(result.report.routes[0].networks, ['CN2', '163']);
  assert.equal(result.report.routes[0].status, 'detected');
  assert.deepEqual(automaticRouteTags(result.report, { ct: 'CT' }, 1001), ['CT CN2 / 163']);
  assert.doesNotMatch(JSON.stringify(result.report), /GIA|GT|203\.0\.113|FAKE_TEST_VALUE/);
  assert.deepEqual(result.report.routes[0].hops[1], []);
});

test('daily route reports tolerate scheduling delays and expire after 48 hours', () => {
  const result = normalizeRouteReport(report(), 1000).report;
  assert.equal(result.expires_at, 1000 + 48 * 3600000);
  assert.deepEqual(automaticRouteTags(result, undefined, 1000 + 24 * 3600000 + 5 * 60000), ['CT CN2 / 163']);
  assert.deepEqual(automaticRouteTags(result, undefined, result.expires_at - 1), ['CT CN2 / 163']);
  assert.deepEqual(automaticRouteTags(result, undefined, result.expires_at), []);
});

test('IPv6, CMI, 9929 and incomplete/unknown/failed evidence keep distinct states', () => {
  const result = normalizeRouteReport(report([
    evidence({ carrier: 'cu', family: 6, hops: [[9929], [4837]] }),
    evidence({ carrier: 'cm', hops: [[58453], [58807], [9808]] }),
    evidence({ completed: false }),
    evidence({ family: 6, hops: [[64500]] }),
    evidence({ carrier: 'cu', completed: false, status: 'failed', hops: [] }),
    evidence({ carrier: 'cm', family: 6, completed: false, status: 'disabled', hops: [] })
  ]), 1000).report;
  assert.deepEqual(result.routes.map(route => route.status), ['detected', 'detected', 'partial', 'unknown', 'failed', 'disabled']);
  assert.deepEqual(result.routes[1].networks, ['CMI', 'CMIN2', 'CMNET']);
  assert.deepEqual(automaticRouteTags(result, undefined, 1001), ['CU 9929 / 4837', 'CM CMI / CMIN2 / CMNET', 'CT CN2 / 163']);
  assert.deepEqual(automaticRouteTags(result, undefined, result.expires_at), []);
});

test('the write boundary rejects malformed, duplicate and contradictory evidence', () => {
  for (const value of [null, {}, report([]), report([evidence(), evidence()]),
    report([evidence({ hops: [[-1]] })]), report([evidence({ hops: Array(31).fill([]) })]),
    report([evidence({ hops: [[4809, 4134, 4837, 9929]] })]),
    report([evidence({ status: 'failed' })]), report([evidence({ family: '4' })])]) {
    assert.throws(() => normalizeRouteReport(value));
  }
});

const miniflare = new Miniflare({ modules: true,
  script: 'export default { fetch() { return new Response("OK") } }',
  d1Databases: { DB: 'route-detection-test', LEGACY: 'route-detection-legacy-test' } });
const db = await miniflare.getD1Database('DB');
const legacy = await miniflare.getD1Database('LEGACY');
const env = { DB: db, API_SECRET: 'LOCAL_ROUTE_TEST_ONLY' };
await initDatabase(db);
await db.prepare("INSERT INTO servers (id, name, tags, custom_ct, custom_cu, ping_mode, history_partition_id) VALUES (?, ?, ?, ?, ?, ?, ?)")
  .bind('routes-demo', 'Demo', 'Manual GIA', 'ct.example.invalid:443', '0', 'icmp', 1).run();
after(async () => { clearAllCaches(); await miniflare.dispose(); });

const request = (payload, authorized = true) => new Request('https://monitor.example.invalid/update/routes', {
  method: 'POST', headers: authorized ? { Authorization: `Bearer ${env.API_SECRET}` } : {},
  body: typeof payload === 'string' ? payload : JSON.stringify(payload)
});

test('agent authentication runs before parsing, and missing server IDs cannot create nodes', async () => {
  assert.equal((await handleRouteDetection(request('not json', false), env)).status, 401);
  assert.equal((await handleRouteDetection(request({ ...report(), id: 'does-not-exist' }), env)).status, 404);
  assert.equal((await db.prepare('SELECT COUNT(*) AS n FROM servers').first()).n, 1);
});

test('configuration inherits site targets, honors disabled nodes and uses the server protocol', async () => {
  const response = await handleRouteDetection(new Request('https://monitor.example.invalid/update/routes?id=routes-demo', {
    headers: { Authorization: `Bearer ${env.API_SECRET}` }
  }), env);
  assert.equal(response.status, 200);
  const config = await response.json();
  assert.equal(config.targets[0].target, 'ct.example.invalid:443');
  assert.equal(config.targets[1].target, '');
  assert.ok(config.targets[2].target);
  assert.ok(config.targets[2].backup[4]);
  assert.equal(config.targets[0].protocol, 'icmp');
  assert.equal(JSON.stringify(config).includes(env.API_SECRET), false);
});

test('three carrier results remain visible through the monthly interval', () => {
  const start = 1000;
  const day = 86400000;
  const full = normalizeRouteReport(report([
    evidence(),
    evidence({ carrier: 'cu', hops: [[4837]], protocol: 'icmp', attempts: 3, target_source: 'backup' }),
    evidence({ carrier: 'cm', hops: [[58453]], completed: false })
  ]), start).report;
  assert.equal(full.routes[1].target_source, 'backup');
  assert.equal(full.expires_at, start + 32 * day);
  assert.equal(automaticRouteTags(full, undefined, start + 30 * day).length, 3);
  const unrelated = normalizeRouteReport(report([
    evidence(), evidence({ carrier: 'cu', hops: [[6453], [4134]] }), evidence({ carrier: 'cm', hops: [[58453]] })
  ]), start).report;
  assert.equal(unrelated.expires_at, start + 2 * day);
});

test('agent config skips traces only before the monthly deadline for the current enabled carriers', async () => {
  const url = 'https://monitor.example.invalid/update/routes?id=routes-demo';
  const headers = { Authorization: `Bearer ${env.API_SECRET}` };
  const firstCheck = await (await handleRouteDetection(new Request(url, { headers }), env)).json();
  assert.deepEqual(firstCheck.covered_families, []);
  const payload = report([
    evidence(),
    evidence({ carrier: 'cu', status: 'disabled', completed: false, hops: [] }),
    evidence({ carrier: 'cm', hops: [[9808]] })
  ]);
  assert.equal((await handleRouteDetection(request(payload), env)).status, 200);
  assert.deepEqual((await (await handleRouteDetection(new Request(url, { headers }), env)).json()).covered_families, [4]);
  const scheduled = normalizeRouteReport(payload, Date.now() - 30 * 86400000).report;
  await db.prepare('UPDATE servers SET route_report = ? WHERE id = ?').bind(JSON.stringify(scheduled), payload.id).run();
  clearAllCaches();
  assert.deepEqual((await (await handleRouteDetection(new Request(url, { headers }), env)).json()).covered_families, []);
  const legacy = { ...scheduled, checked_at: Date.now(), expires_at: Date.now() + 48 * 3600000 };
  await db.prepare('UPDATE servers SET route_report = ? WHERE id = ?').bind(JSON.stringify(legacy), payload.id).run();
  clearAllCaches();
  assert.deepEqual((await (await handleRouteDetection(new Request(url, { headers }), env)).json()).covered_families, []);
});

test('real D1 writes preserve manual tags, replace failed results and invalidate the list cache', async () => {
  await getAllServers(db);
  const response = await handleRouteDetection(request({ ...report(), ip: '203.0.113.8' }), env);
  assert.equal(response.status, 200);
  const row = await db.prepare('SELECT tags, route_report FROM servers WHERE id = ?').bind('routes-demo').first();
  assert.equal(row.tags, 'Manual GIA');
  assert.equal(JSON.parse(row.route_report).routes[0].status, 'detected');
  assert.equal(JSON.parse(row.route_report).expires_at - JSON.parse(row.route_report).checked_at, 48 * 3600000);
  assert.doesNotMatch(row.route_report, /203\.0\.113/);
  assert.equal(JSON.parse((await getAllServers(db))[0].route_report).routes[0].status, 'detected');
  await handleRouteDetection(request(report([evidence({ status: 'failed', completed: false, hops: [] })])), env);
  assert.equal(JSON.parse((await getAllServers(db))[0].route_report).routes[0].status, 'failed');
});

test('direct server detail exposes only the four listing visibility switches without fetching the list', async () => {
  const sys = { is_public: 'true', show_price: 'false', show_expire: 'true', show_tf: 'false', show_three_net_details: 'true' };
  const result = await handleServerAPI(new Request('https://monitor.example.invalid/api/server?id=routes-demo'), env, sys);
  assert.equal(result.status, 200);
  const { sysConfig } = await result.json();
  assert.deepEqual(sysConfig, { long_history_points: 120, show_price: false, show_expire: true, show_tf: false, show_three_net_details: true });
});

test('public list and detail return typed reports while hidden nodes retain access control', async () => {
  const sys = { is_public: 'true', show_three_net_details: 'false' };
  const detail = await (await handleServerAPI(new Request('https://monitor.example.invalid/api/server?id=routes-demo'), env, sys)).json();
  assert.equal(detail.route_report.routes[0].status, 'failed');
  const list = await (await handleServersAPI(new Request('https://monitor.example.invalid/api/servers'), env, sys)).json();
  assert.equal(list.servers[0].route_report.routes[0].status, 'failed');
  await db.prepare("INSERT INTO servers (id, name, is_hidden, history_partition_id) VALUES ('hidden-routes', 'Hidden', '1', 2)").run();
  clearAllCaches();
  assert.equal((await handleServerAPI(new Request('https://monitor.example.invalid/api/server?id=hidden-routes'), env, sys)).status, 404);
  const filtered = await (await handleServersAPI(new Request('https://monitor.example.invalid/api/servers'), env, sys)).json();
  assert.equal(filtered.servers.length, 1);
});

test('actual body length is bounded even without Content-Length; bad JSON does not mutate storage', async () => {
  const before = (await db.prepare('SELECT route_report FROM servers WHERE id = ?').bind('routes-demo').first()).route_report;
  assert.equal((await handleRouteDetection(request(' '.repeat(16385)), env)).status, 413);
  assert.equal((await handleRouteDetection(request('{'), env)).status, 400);
  assert.equal((await db.prepare('SELECT route_report FROM servers WHERE id = ?').bind('routes-demo').first()).route_report, before);
});

test('legacy databases require an additive upgrade that preserves server data', async () => {
  await legacy.prepare("CREATE TABLE servers (id TEXT PRIMARY KEY, name TEXT, tags TEXT, price TEXT DEFAULT '')").run();
  await legacy.prepare("INSERT INTO servers (id, name, tags) VALUES ('legacy-routes', 'Legacy', 'Keep')").run();
  clearAllCaches();
  const legacyEnv = { ...env, DB: legacy };
  const missing = await handleRouteDetection(request({ ...report(), id: 'legacy-routes' }), legacyEnv);
  assert.equal(missing.status, 409);
  assert.equal((await addServerColumns(legacy)).success, true);
  clearAllCaches();
  assert.equal((await handleRouteDetection(request({ ...report(), id: 'legacy-routes' }), legacyEnv)).status, 200);
  const row = await legacy.prepare("SELECT * FROM servers WHERE id = 'legacy-routes'").first();
  assert.equal(row.tags, 'Keep');
  assert.equal(JSON.parse(row.route_report).routes[0].status, 'detected');
});
