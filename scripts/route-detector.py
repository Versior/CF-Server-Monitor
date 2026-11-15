#!/usr/bin/env python3
"""Run NextTrace on the VPS and send ASN-only evidence to CF-Server-Monitor."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class MonitorRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise RuntimeError('Monitor redirects are not allowed; configure the final HTTPS origin')


def load_config(path):
    if os.name == 'posix' and Path(path).stat().st_mode & 0o077:
        raise ValueError('Config must be private: chmod 600 the config file')
    config = json.loads(Path(path).read_text())
    if not isinstance(config, dict):
        raise ValueError('Config must be a JSON object')
    origin = urlsplit(config['url'])
    if (origin.scheme not in ('http', 'https') or not origin.hostname or
            origin.username or origin.password or origin.query or origin.fragment or
            origin.path not in ('', '/') or
            origin.scheme == 'http' and origin.hostname not in ('127.0.0.1', 'localhost', '::1')):
        raise ValueError('Use a clean HTTPS origin; HTTP is only allowed for localhost')
    if (not isinstance(config['id'], str) or not config['id'].strip() or
            not isinstance(config['secret'], str) or not config['secret'] or
            '\n' in config['secret'] or '\r' in config['secret']):
        raise ValueError('Config needs a server ID and an agent credential')
    if (not isinstance(config.get('ua', 'cfsm'), str) or not config.get('ua', 'cfsm') or
            '\n' in config.get('ua', 'cfsm') or '\r' in config.get('ua', 'cfsm')):
        raise ValueError('Config needs a valid agent User-Agent')
    families = config.get('families', [4])
    if families not in ([4], [6], [4, 6]) or any(type(family) is not int for family in families):
        raise ValueError('families must be [4], [6], or [4, 6]')
    executable = shutil.which(config.get('nexttrace', 'nexttrace'))
    if not executable:
        raise ValueError('NextTrace v1.7.6 or later must be installed first')
    return {**config, 'url': config['url'].rstrip('/'), 'id': config['id'].strip(),
            'families': families, 'nexttrace': executable}


def api_request(config, payload=None):
    suffix = '?' + urlencode({'id': config['id']}) if payload is None else ''
    request = Request(config['url'] + '/update/routes' + suffix,
                      data=json.dumps(payload).encode() if payload is not None else None,
                      headers={'Authorization': 'Bearer ' + config['secret'],
                               'User-Agent': config.get('ua', 'cfsm'),
                               'Content-Type': 'application/json'})
    try:
        with build_opener(MonitorRedirect).open(request, timeout=20) as response:
            return json.load(response)
    except HTTPError as error:
        raise RuntimeError(f'Monitor rejected the request: HTTP {error.code}') from None
    except URLError:
        raise RuntimeError('Could not connect to the monitor') from None


def target_address(target):
    if target.startswith('['):
        match = re.fullmatch(r'\[([^]]+)\](?::(\d{1,5}))?', target)
        if not match:
            raise ValueError('Invalid target')
        ipaddress.IPv6Address(match[1])
        return match[1], int(match[2] or 80)
    match = re.fullmatch(r'([a-zA-Z0-9][a-zA-Z0-9._-]*)(?::(\d{1,5}))?', target)
    if not match:
        raise ValueError('Invalid target')
    return match[1], int(match[2] or 80)


def trace_command(executable, target, family, protocol):
    host, port = target_address(target)
    if not 1 <= port <= 65535 or protocol not in ('tcp', 'icmp'):
        raise ValueError('Invalid target protocol or port')
    return [executable, '--traceroute', '--json', '--no-rdns', '--no-color',
            '--queries', '2', '--max-hops', '30', '--timeout', '1500', f'-{family}',
            *(['--tcp', '--port', str(port)] if protocol == 'tcp' else []), host]


def parse_trace(text):
    result = json.loads(text)
    if not isinstance(result, dict) or not isinstance(result.get('Hops'), list) or len(result['Hops']) > 30:
        raise ValueError('Unsupported NextTrace JSON output')
    hops = []
    for samples in result['Hops']:
        if samples is None:
            hops.append([])
            continue
        if not isinstance(samples, list) or len(samples) > 3:
            raise ValueError('Invalid NextTrace hop samples')
        asns = []
        for sample in samples:
            if not isinstance(sample, dict) or sample.get('Geo') is not None and not isinstance(sample['Geo'], dict):
                raise ValueError('Invalid NextTrace sample')
            if not sample.get('Success'):
                continue
            asn = str((sample.get('Geo') or {}).get('asnumber', ''))
            if re.fullmatch(r'(?:AS)?\d+', asn):
                value = int(asn.removeprefix('AS'))
                if 1 <= value <= 4294967295 and value not in asns:
                    asns.append(value)
        hops.append(asns)
    stop = result.get('StopReason') or {}
    if not isinstance(stop, dict):
        raise ValueError('Invalid NextTrace stop reason')
    return {'hops': hops, 'completed': stop.get('reason') == 'destination_reached'}


def collect(config, targets):
    if (not isinstance(targets, list) or len(targets) != 3 or
            any(not isinstance(target, dict) or not isinstance(target.get('target'), str) for target in targets) or
            {target.get('carrier') for target in targets} != {'ct', 'cu', 'cm'}):
        raise ValueError('Monitor returned invalid route targets')
    routes = []
    for target in targets:
        for family in config['families']:
            route = {'carrier': target['carrier'], 'family': family,
                     'status': 'disabled', 'hops': [], 'completed': False}
            if target['target']:
                command = trace_command(config['nexttrace'], target['target'], family, target['protocol'])
                try:
                    process = subprocess.run(command, capture_output=True, text=True, timeout=120,
                                             env={key: value for key, value in os.environ.items() if key != 'NEXTTRACE_UNINTERRUPTED'})
                    if process.returncode:
                        raise ValueError('Trace failed')
                    route.update(parse_trace(process.stdout), status='ok')
                except (subprocess.TimeoutExpired, OSError, ValueError):
                    route['status'] = 'failed'
            routes.append(route)
    return {'id': config['id'], 'routes': routes}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Private JSON config file (mode 600)')
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        targets = api_request(config)['targets']
        report = collect(config, targets)
        api_request(config, report)
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (ValueError, KeyError, OSError):
        print('Route detection failed. Check config permissions, NextTrace, monitor access and database upgrade.', file=sys.stderr)
        return 1
    failed = sum(route['status'] == 'failed' for route in report['routes'])
    print(f'Route report saved: {len(report["routes"])} directions, {failed} failed. Only ASN evidence was sent to the monitor.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
