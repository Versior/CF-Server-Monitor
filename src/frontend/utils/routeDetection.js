export function routeCarrierNames(config, translations) {
  return {
    ct: config?.custom_ct_name || translations.customCt || 'CT',
    cu: config?.custom_cu_name || translations.customCu || 'CU',
    cm: config?.custom_cm_name || translations.customCm || 'CM'
  };
}

export function automaticRouteTags(report, names = { ct: 'CT', cu: 'CU', cm: 'CM' }, now = Date.now()) {
  if (!report || report.expires_at <= now) return [];
  return [...new Set(report.routes.filter(route => route.status === 'detected' ||
      route.status === 'partial' && route.networks.length ||
      route.status === 'unknown' && route.completed && route.transit_networks?.length)
    .map(route => `${names[route.carrier]} ${route.status === 'unknown' ? 'via ' + route.transit_networks.join(' / ') : route.networks.join(' / ')}`))];
}
