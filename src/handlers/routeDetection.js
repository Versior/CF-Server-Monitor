import { buildAgentConfig } from '../utils/agentConfig.js';
import { clearServersListCache, getServerDetail } from '../utils/cache.js';
import { loadSiteSettings } from '../utils/settings.js';
import { createBadRequestResponse, createNotFoundResponse, createSuccessResponse, createUnauthorizedResponse } from '../utils/errors.js';
import { BACKUP_ROUTE_TARGETS, coveredRouteFamilies, normalizeRouteReport, ROUTE_RECHECK_MS, ROUTE_REPORT_MAX_BYTES } from '../utils/routeDetection.js';

export async function handleRouteDetection(request, env) {
  if (request.headers.get('Authorization') !== `Bearer ${env.API_SECRET}`) {
    return createUnauthorizedResponse('Invalid agent credential');
  }
  if (request.method === 'GET') {
    const id = new URL(request.url).searchParams.get('id');
    if (!id) return createBadRequestResponse('Missing ID');
    const server = await getServerDetail(env.DB, id, true);
    if (!server) return createNotFoundResponse('Server not found');
    if (!('route_report' in server)) return new Response('Database upgrade required', { status: 409 });
    const settings = await loadSiteSettings(env.DB);
    const config = buildAgentConfig(server, settings);
    const targets = ['ct', 'cu', 'cm'].map(carrier => ({
      carrier, target: config[`custom_${carrier}`], protocol: config.ping_mode || 'tcp', backup: BACKUP_ROUTE_TARGETS[carrier]
    }));
    const report = server.route_report ? JSON.parse(server.route_report) : null;
    const now = Date.now();
    const covered_families = report && report.expires_at - report.checked_at > ROUTE_RECHECK_MS && now - report.checked_at < ROUTE_RECHECK_MS
      ? coveredRouteFamilies(report.routes, targets.filter(item => item.target).map(item => item.carrier))
      : [];
    return createSuccessResponse({ targets, covered_families }, { 'Cache-Control': 'no-store' });
  }

  const reader = request.body?.getReader();
  if (!reader) return createBadRequestResponse('Missing route report');
  const chunks = [];
  let length = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    length += value.byteLength;
    if (length > ROUTE_REPORT_MAX_BYTES) {
      await reader.cancel();
      return new Response('Route report too large', { status: 413 });
    }
    chunks.push(value);
  }
  const bytes = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  let normalized;
  try {
    normalized = normalizeRouteReport(JSON.parse(new TextDecoder().decode(bytes)));
  } catch {
    return createBadRequestResponse('Invalid route report');
  }
  const server = await getServerDetail(env.DB, normalized.id, true);
  if (!server) return createNotFoundResponse('Server not found');
  if (!('route_report' in server)) return new Response('Database upgrade required', { status: 409 });
  const result = await env.DB.prepare('UPDATE servers SET route_report = ? WHERE id = ?')
    .bind(JSON.stringify(normalized.report), normalized.id).run();
  clearServersListCache();
  if (!result.meta.changes) return createNotFoundResponse('Server not found');
  return createSuccessResponse({ success: true, checked_at: normalized.report.checked_at }, { 'Cache-Control': 'no-store' });
}
