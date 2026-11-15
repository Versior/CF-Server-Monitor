export function automaticRouteTags(report, names = { ct: 'CT', cu: 'CU', cm: 'CM' }, now = Date.now()) {
  if (!report || report.expires_at <= now) return [];
  return [...new Set(report.routes.filter(route => route.status === 'detected')
    .map(route => `${names[route.carrier]} ${route.networks.join(' / ')}`))];
}
