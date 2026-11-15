import registry from '../../scripts/route-networks.json' with { type: 'json' };

const NETWORKS = new Map(Object.entries(registry.asn_networks).map(([asn, name]) => [Number(asn), name]));
const TRANSIT_NETWORKS = new Map(Object.entries(registry.transit_networks).map(([asn, name]) => [Number(asn), name]));
const PREFIX_NETWORKS = new Map(Object.entries(registry.prefix_networks));
export const ROUTE_REPORT_MAX_BYTES = 16384;
export const ROUTE_REPORT_TTL_MS = 48 * 3600000;

// Exact hop addresses, router names and credentials never enter storage.
export function normalizeRouteReport(raw, now = Date.now()) {
  if (!raw || typeof raw.id !== 'string' || !raw.id.trim() || raw.id.length > 128 ||
      !Array.isArray(raw.routes) || raw.routes.length < 1 || raw.routes.length > 6) {
    throw new Error('Invalid route report');
  }
  const seen = new Set();
  const routes = raw.routes.map(route => {
    if (!route || !['ct', 'cu', 'cm'].includes(route.carrier) || ![4, 6].includes(route.family) ||
        !['ok', 'failed', 'disabled'].includes(route.status) || typeof route.completed !== 'boolean' ||
        !Array.isArray(route.hops) || route.hops.length > 30 ||
        route.hops.some(hop => !Array.isArray(hop) || hop.length > 3 ||
          hop.some(asn => !Number.isInteger(asn) || asn < 1 || asn > 4294967295))) {
      throw new Error('Invalid route evidence');
    }
    const key = `${route.carrier}-${route.family}`;
    if (seen.has(key)) throw new Error('Duplicate route direction');
    seen.add(key);
    const hints = route.prefix_hints ?? [];
    if (!Array.isArray(hints) || hints.length > PREFIX_NETWORKS.size || hints.some(hint => !hint ||
      !Number.isInteger(hint.hop) || hint.hop < 0 || hint.hop >= route.hops.length || !PREFIX_NETWORKS.has(hint.prefix)) ||
      ((route.protocol !== undefined || route.attempts !== undefined) && (!['tcp', 'icmp'].includes(route.protocol) ||
        !Number.isInteger(route.attempts) || route.attempts < 0 || route.attempts > 2))) {
      throw new Error('Invalid route hint or probe metadata');
    }
    if (route.status !== 'ok' && (route.completed || route.hops.length || hints.length)) {
      throw new Error('Failed or disabled routes cannot contain evidence');
    }
    const hops = route.hops.map(hop => [...new Set(hop)]);
    const prefix_hints = hints.map(hint => ({ hop: hint.hop, prefix: hint.prefix, network: PREFIX_NETWORKS.get(hint.prefix) }));
    const networks = [...new Set([...hops.flat().map(asn => NETWORKS.get(asn)).filter(Boolean), ...prefix_hints.map(hint => hint.network)])];
    return {
      carrier: route.carrier,
      family: route.family,
      status: route.status !== 'ok' ? route.status : !networks.length ? 'unknown' : route.completed ? 'detected' : 'partial',
      completed: route.completed,
      networks,
      hops,
      prefix_hints,
      transit_networks: [...new Set(hops.flat().map(asn => TRANSIT_NETWORKS.get(asn)).filter(Boolean))],
      ...(route.protocol !== undefined ? { protocol: route.protocol, attempts: route.attempts } : {})
    };
  });
  return { id: raw.id.trim(), report: { checked_at: now, expires_at: now + ROUTE_REPORT_TTL_MS, routes } };
}
