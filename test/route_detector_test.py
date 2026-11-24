import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('detector', Path(__file__).parents[1] / 'scripts/route-detector.py')
detector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(detector)


class RouteDetectorTests(unittest.TestCase):
    def collect_fixture(self, primary, alternate):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / 'nexttrace'
            log = Path(folder) / 'commands.jsonl'
            executable.write_text(f'#!{sys.executable}\nimport json, sys\nfrom pathlib import Path\n'
                                  f'with Path({str(log)!r}).open("a") as f: f.write(json.dumps(sys.argv[1:])+"\\n")\n'
                                  f'payload = {primary!r} if "--tcp" in sys.argv else {alternate!r}\n'
                                  'if payload is None: sys.exit(1)\nprint(json.dumps(payload))\n')
            executable.chmod(0o755)
            result = detector.collect({'id': 'demo', 'families': [4], 'nexttrace': str(executable)},
                                      [{'carrier': carrier, 'target': 'ct.example.invalid:443' if carrier == 'ct' else '',
                                        'protocol': 'tcp'} for carrier in ('ct', 'cu', 'cm')])
            return result, [json.loads(line) for line in log.read_text().splitlines()]

    def test_router_prefix_recovers_missing_geo_without_fabricating_an_asn(self):
        result = detector.parse_trace(json.dumps({'Hops': [
            [{'Success': True, 'Address': {'IP': '59.43.1.1'}, 'Geo': None}],
            [{'Success': True, 'Address': {'IP': '59.43.2.1'}, 'Geo': {'asnumber': '4134'}}],
            [{'Success': False, 'Address': {'IP': '202.97.1.1'}}],
            [{'Success': True, 'Address': {'IP': '203.0.113.8'}}]
        ], 'StopReason': {'reason': 'destination_reached'}}))
        self.assertEqual(result['hops'], [[], [4134], [], []])
        self.assertEqual(result['prefix_hints'], [{'hop': 0, 'prefix': '59.43.0.0/16'}])
        self.assertNotIn('59.43.1.1', json.dumps(result))

    def test_prefix_matching_uses_network_boundaries_and_specific_cmin2_ranges(self):
        result = detector.parse_trace(json.dumps({'Hops': [
            [{'Success': True, 'Address': {'IP': '59.430.1.1'}}],
            [{'Success': True, 'Address': {'IP': '223.120.128.1'}}],
            [{'Success': True, 'Address': {'IP': '223.120.1.1'}}],
            [{'Success': True, 'Address': {'IP': '2001:db8::1'}}]
        ]}))
        self.assertEqual(result['prefix_hints'], [{'hop': 1, 'prefix': '223.120.128.0/17'},
                                                 {'hop': 2, 'prefix': '223.120.0.0/16'}])

    def test_explicit_cmin2_asn_is_not_overridden_by_a_broad_cmi_prefix(self):
        result = detector.parse_trace(json.dumps({'Hops': [[{'Success': True,
            'Address': {'IP': '.'.join(map(str, (223, 119, 8, 1)))}, 'Geo': {'asnumber': '58807'}}]]}))
        self.assertEqual(result['hops'], [[58807]])
        self.assertEqual(result['prefix_hints'], [])

    def test_sparse_tcp_trace_retries_icmp_and_keeps_one_real_path(self):
        primary = {'Hops': [[{'Success': True, 'Geo': {'asnumber': '6453'}}]],
                   'StopReason': {'reason': 'destination_reached'}}
        alternate = {'Hops': [[{'Success': True, 'Address': {'IP': '59.43.1.1'}, 'Geo': None}]],
                     'StopReason': {'reason': 'destination_reached'}}
        result, commands = self.collect_fixture(primary, alternate)
        route = result['routes'][0]
        self.assertEqual(len(commands), 2)
        self.assertIn('--tcp', commands[0])
        self.assertNotIn('--tcp', commands[1])
        self.assertEqual(route['protocol'], 'icmp')
        self.assertEqual(route['attempts'], 2)
        self.assertEqual(route['hops'], [[]])
        self.assertEqual(route['prefix_hints'], [{'hop': 0, 'prefix': '59.43.0.0/16'}])

    def test_international_only_trace_tries_same_carrier_backup_and_keeps_one_path(self):
        international = {'Hops': [[{'Success': True, 'Geo': {'asnumber': '6453'}}]],
                         'StopReason': {'reason': 'destination_reached'}}
        identified = {'Hops': [[{'Success': True, 'Geo': {'asnumber': '4837'}}]],
                      'StopReason': {'reason': 'destination_reached'}}
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / 'nexttrace'
            executable.write_text(f'#!{sys.executable}\nimport json, sys\n'
                                  f'print(json.dumps({international!r} if sys.argv[-1] == "cu.example.invalid" else {identified!r}))\n')
            executable.chmod(0o755)
            result = detector.collect({'id': 'demo', 'families': [4], 'nexttrace': str(executable)}, [
                {'carrier': 'ct', 'target': '', 'protocol': 'tcp'},
                {'carrier': 'cu', 'target': 'cu.example.invalid:443', 'protocol': 'tcp',
                 'backup': {'4': 'backup.example.invalid'}},
                {'carrier': 'cm', 'target': '', 'protocol': 'tcp'},
            ])
        route = result['routes'][1]
        self.assertEqual((route['attempts'], route['target_source'], route['hops']), (3, 'backup', [[4837]]))
        self.assertEqual(result['routes'][0]['attempts'], 0)

    def test_current_three_carrier_reports_skip_traces_and_upload(self):
        for covered, options, should_run in (([4], [], False), ([], [], True), ([4], ['--force'], True)):
            with self.subTest(covered=covered, options=options), \
                    patch.object(sys, 'argv', ['route-detector', '--config', 'test.json', *options]), \
                    patch.object(detector, 'load_config', return_value={'families': [4]}), \
                    patch.object(detector, 'api_request', return_value={'targets': [], 'covered_families': covered}) as api, \
                    patch.object(detector, 'collect', return_value={'routes': []}) as collect:
                self.assertEqual(detector.main(), 0)
                self.assertEqual(collect.call_count, int(should_run))
                self.assertEqual(api.call_count, 1 + int(should_run))

    def test_good_primary_path_stops_without_retry_and_failure_does_not_erase_evidence(self):
        good = {'Hops': [[{'Success': True, 'Geo': {'asnumber': '4809'}}]],
                'StopReason': {'reason': 'destination_reached'}}
        result, commands = self.collect_fixture(good, None)
        self.assertEqual(len(commands), 1)
        self.assertEqual(result['routes'][0]['attempts'], 1)
        sparse = {'Hops': [[{'Success': True, 'Geo': {'asnumber': '4134'}}], None, None],
                  'StopReason': {'reason': 'destination_reached'}}
        result, commands = self.collect_fixture(sparse, None)
        self.assertEqual(len(commands), 2)
        self.assertEqual(result['routes'][0]['status'], 'ok')
        self.assertEqual(result['routes'][0]['hops'], [[4134], [], []])

    def test_monitor_redirects_cannot_forward_agent_credentials(self):
        with self.assertRaises(RuntimeError):
            detector.MonitorRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example.invalid')

    def test_nexttrace_v176_json_retains_holes_and_only_exports_asns(self):
        source = {'Hops': [
            [{'Success': True, 'Address': {'IP': '203.0.113.7'}, 'Geo': {'asnumber': '4809', 'ip': '203.0.113.7'}},
             {'Success': True, 'Geo': {'asnumber': '4809'}}],
            [{'Success': False, 'Geo': {'asnumber': '4134'}}],
            None,
            [{'Success': True, 'Geo': {'asnumber': 'AS4837'}}]
        ], 'StopReason': {'reason': 'destination_reached'}}
        result = detector.parse_trace(json.dumps(source))
        self.assertEqual(result, {'hops': [[4809], [], [], [4837]], 'completed': True, 'prefix_hints': []})
        self.assertNotIn('203.0.113', json.dumps(result))

    def test_incomplete_trace_and_missing_geo_do_not_become_successful_detection(self):
        result = detector.parse_trace(json.dumps({'Hops': [[{'Success': True, 'Geo': None}]], 'StopReason': {'reason': 'max_hops'}}))
        self.assertEqual(result, {'hops': [[]], 'completed': False, 'prefix_hints': []})
        for value in [[], {}, {'Hops': [[None]]}, {'Hops': [], 'StopReason': 'bad'}]:
            with self.assertRaises(ValueError):
                detector.parse_trace(json.dumps(value))

    def test_finite_traceroute_tcp_ipv6_and_icmp_commands(self):
        command = detector.trace_command('/usr/bin/nexttrace', '[2001:db8::1]:443', 6, 'tcp')
        self.assertIn('--traceroute', command)
        self.assertIn('--json', command)
        self.assertIn('-6', command)
        self.assertEqual(command[-4:], ['--tcp', '--port', '443', '2001:db8::1'])
        self.assertNotIn('--tcp', detector.trace_command('/usr/bin/nexttrace', 'ct.example.invalid', 4, 'icmp'))
        for target in ['--help', 'https://example.invalid', 'example.invalid;echo', 'example.invalid:0']:
            with self.assertRaises(ValueError):
                detector.trace_command('/usr/bin/nexttrace', target, 4, 'tcp')

    def test_private_config_and_no_credentials_in_origin(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps({'url': 'https://example-user@example.invalid', 'id': 'demo', 'secret': 'FAKE'}))
            path.chmod(0o600)
            with self.assertRaises(ValueError):
                detector.load_config(path)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                detector.load_config(path)

    def test_agent_user_agent_is_validated_at_the_config_boundary(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            config = {'url': 'https://monitor.example.invalid', 'id': 'demo', 'secret': 'FAKE',
                      'nexttrace': '/bin/sh', 'ua': 'cfsm-custom-test'}
            path.write_text(json.dumps(config))
            path.chmod(0o600)
            self.assertEqual(detector.load_config(path)['ua'], 'cfsm-custom-test')
            config['ua'] = 'bad\r\nheader'
            path.write_text(json.dumps(config))
            with self.assertRaises(ValueError):
                detector.load_config(path)


if __name__ == '__main__':
    unittest.main()
