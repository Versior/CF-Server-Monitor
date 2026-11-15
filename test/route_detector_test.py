import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('detector', Path(__file__).parents[1] / 'scripts/route-detector.py')
detector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(detector)


class RouteDetectorTests(unittest.TestCase):
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
        self.assertEqual(result, {'hops': [[4809], [], [], [4837]], 'completed': True})
        self.assertNotIn('203.0.113', json.dumps(result))

    def test_incomplete_trace_and_missing_geo_do_not_become_successful_detection(self):
        result = detector.parse_trace(json.dumps({'Hops': [[{'Success': True, 'Geo': None}]], 'StopReason': {'reason': 'max_hops'}}))
        self.assertEqual(result, {'hops': [[]], 'completed': False})
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
