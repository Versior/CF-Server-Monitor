import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('installer', ROOT / 'scripts/install-probe.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)
detector_spec = importlib.util.spec_from_file_location('detector', ROOT / 'scripts/route-detector.py')
detector = importlib.util.module_from_spec(detector_spec)
detector_spec.loader.exec_module(detector)


class ProbeInstallerTests(unittest.TestCase):
    def test_missing_published_file_reports_http_status_instead_of_network_failure(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'route-networks.json'
                with self.assertRaisesRegex(RuntimeError, r'route-networks\.json.*HTTP 404') as caught:
                    installer.download(f'http://127.0.0.1:{server.server_port}/route-networks.json', path)
                self.assertNotIn('VPS 到 GitHub 的网络', str(caught.exception))
                self.assertFalse(path.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_download_verifies_release_bytes_before_execution(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'release fixture')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'binary'
                url = f'http://127.0.0.1:{server.server_port}/binary'
                installer.download(url, path, hashlib.sha256(b'release fixture').hexdigest())
                self.assertEqual(path.read_bytes(), b'release fixture')
                with self.assertRaisesRegex(RuntimeError, 'SHA256'):
                    installer.download(url, path, '0' * 64)
                self.assertFalse(path.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_existing_probe_config_is_parsed_without_evaluating_shell(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.conf'
            marker = Path(folder) / 'must-not-exist'
            content = f'# old probe\nSERVER_ID="demo"\nSECRET="FAKE=SECRET"\nNAME="$(touch {marker})"\nWORKER_URL="https://monitor.example.invalid/update"\n'
            path.write_text(content)
            self.assertEqual(installer.read_agent_config(path)['SECRET'], 'FAKE=SECRET')
            self.assertEqual(path.read_text(), content)
            self.assertFalse(marker.exists())

    def test_non_root_probe_blocks_installation_before_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / '.cf-probe').mkdir()
            original = b'Existing private configuration'
            (root / '.cf-probe/config.conf').write_bytes(original)
            account = installer.pwd.struct_passwd(('fixture-user', 'x', 1000, 1000, '', str(root), '/bin/sh'))
            with patch.object(installer.pwd, 'getpwall', return_value=[account]):
                with self.assertRaisesRegex(RuntimeError, '非 root'):
                    installer.check_existing_probes()
            self.assertEqual((root / '.cf-probe/config.conf').read_bytes(), original)

    def test_second_system_instance_blocks_installation(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'cf-probe-second.service').touch()
            with patch.multiple(installer, AGENT_CONFIG=root / 'config/cf-probe/config.conf', SYSTEMD_DIR=root), \
                    patch.object(installer.pwd, 'getpwall', return_value=[]):
                with self.assertRaisesRegex(RuntimeError, '多实例'):
                    installer.check_existing_probes()

    def test_error_output_redacts_credentials(self):
        with self.assertRaises(RuntimeError) as caught:
            installer.run([sys.executable, '-c', 'import sys; sys.stderr.write("FAKE_SENSITIVE_VALUE"); sys.exit(1)'], 'FAKE_SENSITIVE_VALUE')
        self.assertNotIn('FAKE_SENSITIVE_VALUE', str(caught.exception))
        self.assertIn('<REDACTED>', str(caught.exception))

    def test_preflight_rejects_old_worker_missing_migration_and_wrong_credentials(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.server.received_auth = self.headers.get('Authorization')
                self.server.received_ua = self.headers.get('User-Agent')
                self.send_response(self.server.response_status)
                self.end_headers()
                self.wfile.write(self.server.response_body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = {'url': f'http://127.0.0.1:{server.server_port}', 'id': 'demo', 'secret': 'FAKE_SECRET', 'ua': 'cfsm-custom-test'}
        try:
            for status, body, message in (
                    (200, b'<html>Old theme</html>', 'Worker'),
                    (409, b'Database upgrade required', '数据库升级'),
                    (401, b'Unauthorized', 'HTTP 401'),
                    (200, b'{"targets": []}', '配置无效')):
                with self.subTest(status=status, body=body):
                    server.response_status, server.response_body = status, body
                    with self.assertRaisesRegex(RuntimeError, message):
                        installer.check_monitor(detector, config)
            server.response_status = 200
            server.response_body = json.dumps({'targets': [
                {'carrier': carrier, 'target': '', 'protocol': 'tcp'} for carrier in ('ct', 'cu', 'cm')
            ]}).encode()
            installer.check_monitor(detector, config)
            self.assertEqual(server.received_auth, 'Bearer FAKE_SECRET')
            self.assertEqual(server.received_ua, 'cfsm-custom-test')
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_combined_install_reuses_probe_and_writes_private_route_config(self):
        for existing in (False, True):
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                stage = root / 'stage'
                stage.mkdir()
                for name in ('route-detector.py', 'route-detector.service', 'route-detector.timer', 'route-networks.json'):
                    (stage / name).write_bytes((ROOT / 'scripts' / name).read_bytes())
                (stage / 'nexttrace').write_text('fixture binary')
                command_log = root / 'commands'
                executable = '#!/bin/sh\nprintf "%s\\n" "$*" >> "$CFSM_TEST_COMMAND_LOG"\n'
                (stage / 'cf-probe').write_text(executable)
                (stage / 'cf-probe').chmod(0o755)
                (stage / 'systemctl').write_text(executable)
                (stage / 'systemctl').chmod(0o755)
                agent_config = root / 'agent/config.conf'
                agent_config.parent.mkdir()
                systemd = root / 'systemd'
                systemd.mkdir()
                original = 'SERVER_ID="demo"\nSECRET="FAKE_SECRET"\nNAME="Preserved"\n'
                if existing:
                    agent_config.write_text(original)
                    (systemd / 'cfsm-route-detector.timer').touch()
                values = {'SERVER_ID': 'demo', 'SECRET': 'FAKE_SECRET', 'WORKER_URL': 'https://monitor.example.invalid/update'}
                config = {'id': 'demo', 'secret': 'FAKE_SECRET', 'url': 'https://monitor.example.invalid', 'families': [4, 6]}
                with patch.multiple(installer, AGENT_CONFIG=agent_config, ROUTE_DIR=root / 'routes',
                                    ROUTE_CONFIG=root / 'routes.json', SYSTEMD_DIR=systemd), \
                        patch.dict(os.environ, {'PATH': str(stage) + os.pathsep + os.environ['PATH'], 'CFSM_TEST_COMMAND_LOG': str(command_log)}), \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    installer.install(config, values, stage, probe_needed=not existing)
                    installer.install(config, values, stage, probe_needed=False)
                commands = command_log.read_text().splitlines()
                self.assertEqual(commands.count('install'), 0 if existing else 1)
                self.assertNotIn('FAKE_SECRET', command_log.read_text() + output.getvalue())
                self.assertIn('enable --now cf-probe.service', commands)
                self.assertIn('start cfsm-route-detector.service', commands)
                self.assertLess(commands.index('enable --now cf-probe.service'), commands.index('start cfsm-route-detector.service'))
                self.assertEqual(agent_config.read_text(), original if existing else ''.join(f'{key}="{value}"\n' for key, value in values.items()))
                self.assertEqual((root / 'routes.json').stat().st_mode & 0o777, 0o600)
                self.assertEqual(json.loads((root / 'routes.json').read_text())['families'], [4, 6])
                self.assertEqual((root / 'routes/nexttrace').stat().st_mode & 0o777, 0o755)
                self.assertEqual((root / 'routes/route-networks.json').read_bytes(), (ROOT / 'scripts/route-networks.json').read_bytes())
                self.assertIn(sys.executable, (systemd / 'cfsm-route-detector.service').read_text())
                self.assertIn('OnUnitActiveSec=24h\n', (systemd / 'cfsm-route-detector.timer').read_text())
                self.assertEqual(sorted(path.name for path in systemd.iterdir()), ['cfsm-route-detector.service', 'cfsm-route-detector.timer'])
                if existing:
                    self.assertIn('stop cfsm-route-detector.timer cfsm-route-detector.service', commands)


if __name__ == '__main__':
    unittest.main()
