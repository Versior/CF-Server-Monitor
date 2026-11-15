const NETWORKS = new Map([
  [4809, 'CN2'], [4134, '163'], [4837, '4837'], [9929, '9929'],
  [58453, 'CMI'], [58807, 'CMI'], [9808, 'CMNET']
]);
export const ROUTE_REPORT_MAX_BYTES = 16384;
export const ROUTE_REPORT_TTL_MS = 48 * 3600000;

// Report only ASN evidence. Addresses, router names and credentials never enter storage.
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
    if (route.status !== 'ok' && (route.completed || route.hops.length)) {
      throw new Error('Failed or disabled routes cannot contain evidence');
    }
    const hops = route.hops.map(hop => [...new Set(hop)]);
    const networks = [...new Set(hops.flat().map(asn => NETWORKS.get(asn)).filter(Boolean))];
    return {
      carrier: route.carrier,
      family: route.family,
      status: route.status !== 'ok' ? route.status : !networks.length ? 'unknown' : route.completed ? 'detected' : 'partial',
      completed: route.completed,
      networks,
      hops
    };
  });
  return { id: raw.id.trim(), report: { checked_at: now, expires_at: now + ROUTE_REPORT_TTL_MS, routes } };
}
