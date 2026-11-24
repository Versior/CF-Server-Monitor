export function automaticRouteTags(report, names = { ct: 'CT', cu: 'CU', cm: 'CM' }, now = Date.now()) {
  if (!report || report.expires_at <= now) return [];
  return [...new Set(report.routes.filter(route => route.status === 'detected' ||
      route.status === 'partial' && route.networks.length ||
      route.status === 'unknown' && route.completed && route.transit_networks?.length)
    .map(route => `${names[route.carrier]} ${route.status === 'unknown' ? 'via ' + route.transit_networks.join(' / ') : route.networks.join(' / ')}`))];
}
