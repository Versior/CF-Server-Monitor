<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useTranslation } from '../utils/i18n.js'
import { routeCarrierNames } from '../utils/routeDetection.js'

const props = defineProps({ report: { type: Object, default: null }, config: { type: Object, default: null } })
const trans = useTranslation()
const carrierNames = computed(() => routeCarrierNames(props.config, trans.value))
const now = ref(Date.now())
const runtime = { timer: null }
onMounted(() => { runtime.timer = setInterval(() => { now.value = Date.now() }, 60000) })
onUnmounted(() => clearInterval(runtime.timer))
const expired = computed(() => props.report && props.report.expires_at <= now.value)
const statusKey = { unknown: 'routeUnknown', failed: 'routeFailed', disabled: 'routeDisabled' }
</script>

<template>
  <section v-if="report" class="route-detection" :aria-label="trans.routeDetection">
    <header><strong>{{ trans.routeDetection }}</strong><time>{{ new Date(report.checked_at).toLocaleString() }}</time></header>
    <p>{{ trans.routeScope }}</p>
    <div v-for="route in report.routes" :key="`${route.carrier}-${route.family}`" class="route-direction">
      <span>{{ carrierNames[route.carrier] }} · IPv{{ route.family }}<small v-if="route.protocol" class="route-method">{{ trans.routeMethod }}: {{ route.protocol.toUpperCase() }} · {{ trans.routeAttempts }}: {{ route.attempts }}<template v-if="route.target_source === 'backup'"> · {{ trans.routeBackup }}</template></small></span>
      <strong :class="{ detected: !expired && route.networks.length > 0 }">
        <template v-if="expired">{{ trans.routeExpired }}<template v-if="route.networks.length"> · {{ route.networks.join(' / ') }}</template></template>
        <template v-else>{{ route.networks.join(' / ') || trans[statusKey[route.status] || 'routeUnknown'] }}</template>
      </strong>
      <small v-if="route.hops.some(hop => hop.length)">ASN: {{ route.hops.map(hop => hop.length ? hop.map(asn => `AS${asn}`).join('|') : '*').join(' → ') }}</small>
      <small v-if="route.prefix_hints?.length">{{ trans.routeIpPrefix }}: {{ route.prefix_hints.map(hint => `${hint.network} (${hint.prefix})`).join(' / ') }}</small>
      <small v-if="route.transit_networks?.length">{{ trans.routeTransit }}: {{ route.transit_networks.join(' / ') }}</small>
    </div>
  </section>
</template>

<style scoped>
.route-detection{margin:16px 0;padding:16px;border:1px solid var(--border-color);border-radius:12px;background:var(--bg-secondary)}
header{display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap}
time,p,small{font-size:12px;color:var(--text-muted)}
p{margin:8px 0 12px;line-height:1.6}
.route-direction{display:grid;grid-template-columns:minmax(100px,1fr) 2fr;gap:8px;padding:10px 0;border-top:1px solid var(--border-color);font-size:13px}
.route-direction strong{text-align:right;font-weight:500}
.route-direction small{grid-column:1/-1;overflow-wrap:anywhere;line-height:1.6}
.route-method{display:block;margin-top:4px}
.detected{color:var(--accent-green,#00a585)}
@media(max-width:480px){.route-direction{grid-template-columns:1fr}.route-direction strong{text-align:left}}
</style>
