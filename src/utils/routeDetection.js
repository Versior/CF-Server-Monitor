import registry from '../../scripts/route-networks.json' with { type: 'json' };

const NETWORKS = new Map(Object.entries(registry.asn_networks).map(([asn, name]) => [Number(asn), name]));
const TRANSIT_NETWORKS = new Map(Object.entries(registry.transit_networks).map(([asn, name]) => [Number(asn), name]));
const PREFIX_NETWORKS = new Map(Object.entries(registry.prefix_networks));
const CARRIERS = Object.keys(registry.carriers);
const FAMILIES = [4, 6];
const DAY_MS = 86400000;
export const ROUTE_REPORT_MAX_BYTES = 16384;
export const ROUTE_RECHECK_MS = 30 * DAY_MS;
export const BACKUP_ROUTE_TARGETS = registry.backup_targets;

export function coveredRouteFamilies(routes, enabled) {
  return FAMILIES.filter(family => CARRIERS.every(carrier => {
    const route = routes.find(item => item.carrier === carrier && item.family === family);
    if (!route) return false;
    if (!enabled.includes(carrier)) return route.status === 'disabled';
    return route.networks.some(network => registry.carriers[carrier].includes(network));
  }));
}

// Exact hop addresses, router names and credentials never enter storage.
export function normalizeRouteReport(raw, now = Date.now()) {
  if (!raw || typeof raw.id !== 'string' || !raw.id.trim() || raw.id.length > 128 ||
      !Array.isArray(raw.routes) || raw.routes.length < 1 || raw.routes.length > 6) {
    throw new Error('Invalid route report');
  }
  const seen = new Set();
  const routes = raw.routes.map(route => {
    if (!route || !CARRIERS.includes(route.carrier) || !FAMILIES.includes(route.family) ||
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
        !Number.isInteger(route.attempts) || route.attempts < 0 || route.attempts > 3)) ||
      (route.target_source !== undefined && (route.target_source !== 'backup' || route.protocol !== 'icmp' || route.attempts !== 3 || route.status !== 'ok'))) {
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
      ...(route.protocol !== undefined ? { protocol: route.protocol, attempts: route.attempts } : {}),
      ...(route.target_source ? { target_source: route.target_source } : {})
    };
  });
  const families = new Set(routes.map(route => route.family));
  const enabled = CARRIERS.filter(carrier => routes.some(route => route.carrier === carrier && route.status !== 'disabled'));
  const covered = coveredRouteFamilies(routes, enabled).length === families.size;
  return { id: raw.id.trim(), report: { checked_at: now, expires_at: now + 2 * DAY_MS + (covered ? ROUTE_RECHECK_MS : 0), routes } };
}
