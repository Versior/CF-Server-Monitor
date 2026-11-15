#!/usr/bin/env python3
"""Install the Go monitoring probe and ASN route detector through one entry point."""
import argparse
import getpass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import pwd
import shutil
import subprocess
import sys
import tempfile
from urllib.error import URLError
from urllib.request import urlopen

AGENT_CONFIG = Path('/etc/config/cf-probe/config.conf')
AGENT_BINARY = Path('/usr/local/bin/cf-probe')
ROUTE_DIR = Path('/opt/cfsm-route-detector')
ROUTE_CONFIG = Path('/etc/cfsm-route-detector.json')
SYSTEMD_DIR = Path('/etc/systemd/system')
SOURCE = 'https://raw.githubusercontent.com/Versior/CF-Server-Monitor/2484639f7c8c141d037a362ed0b7627cfe28ca95/scripts'
AGENT_VERSION = 'v1.1.3'
NEXTTRACE_VERSION = 'v1.7.6'
HASHES = {
    'amd64': ('711326c3f2d7e41919e0922d8dcc12d694df95c92a3b5db4098c9742c632d405',
              '3868c8190c961b534b8f2d0ed528796fc48d46a818ee5268fe2dfc6aa44b3a5e'),
    'arm64': ('82a7815a047053ed6ad5557d6499ad05e14f5335dae7cbaf8df62211743adbe0',
              'e4c4e818f6cebf8d8ee3df3976a304c8bac2bb3bea3160a23fd5bce18095c9e6'),
}


def download(url, path, digest=None):
    try:
        with urlopen(url, timeout=180) as response, path.open('wb') as output:
            shutil.copyfileobj(response, output)
    except (URLError, OSError):
        raise RuntimeError(f'下载 {path.name} 失败，请检查 VPS 到 GitHub 的网络') from None
    if digest and hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        path.unlink()
        raise RuntimeError(f'{path.name} 的 SHA256 不匹配，已停止安装')


def read_agent_config(path):
    values = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip().strip('"')
    return values


def check_existing_probes():
    for account in pwd.getpwall():
        home = Path(account.pw_dir)
        if any(path.exists() for path in (home / '.cf-probe/config.conf', home / '.cf-probe/bin/cf-probe',
                home / '.config/systemd/user/cf-probe.service')):
            raise RuntimeError('检测到非 root 用户版监控探针，已停止安装，避免重复上报')
    if (Path('/usr/local/bin/cf-probe.sh').exists() or
            list(AGENT_CONFIG.parent.parent.glob('cf-probe-*')) or
            list(SYSTEMD_DIR.glob('cf-probe-*.service'))):
        raise RuntimeError('检测到旧 Shell 或多实例监控探针，已停止安装，请先确认已有安装方式')
    processes = 0
    for path in Path('/proc').glob('[0-9]*/cmdline'):
        try:
            args = path.read_bytes().split(b'\0')
        except (FileNotFoundError, ProcessLookupError):
            continue
        if not any(Path(os.fsdecode(arg)).name == 'cf-probe' for arg in args if arg):
            continue
        processes += 1
        if os.fsdecode(args[0]) != str(AGENT_BINARY) or processes > 1:
            raise RuntimeError('检测到其他监控探针进程，已停止安装，避免出现两个监控探针')
    if shutil.which('docker'):
        try:
            result = subprocess.run(['docker', 'ps', '--format', '{{.Image}} {{.Command}}', '--no-trunc'],
                                    capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            raise RuntimeError('Docker 检查超时，请先确认 Docker 服务状态后重新运行') from None
        if result.returncode:
            raise RuntimeError('无法检查 Docker 探针，请先确认 Docker 服务状态后重新运行')
        if 'cfsm-agent' in result.stdout or 'cf-probe' in result.stdout:
            raise RuntimeError('检测到 Docker 监控探针，已停止安装，避免重复上报')


def private_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
        temporary = Path(output.name)
        output.write(data.encode())
    temporary.chmod(0o600)
    temporary.replace(path)


def run(command, secret, timeout=120):
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError(f'{Path(command[0]).name} 未能完成，请检查服务状态后重新运行') from None
    if result.returncode:
        details = (result.stderr or result.stdout).strip().replace(secret, '<REDACTED>')
        raise RuntimeError(f'{Path(command[0]).name} 执行失败：{details}')


def check_monitor(detector, config):
    try:
        targets = detector.api_request(config)['targets']
    except json.JSONDecodeError:
        raise RuntimeError('站点返回了网页，请先更新对应 Worker，统一安装已停止') from None
    except RuntimeError as error:
        if 'HTTP 409' in str(error):
            raise RuntimeError('请先在监控后台执行数据库升级，统一安装已停止') from None
        raise
    if (not isinstance(targets, list) or len(targets) != 3 or
            {item.get('carrier') for item in targets if isinstance(item, dict)} != {'ct', 'cu', 'cm'} or
            any(not isinstance(item.get('target'), str) or item.get('protocol') not in ('tcp', 'icmp') for item in targets)):
        raise RuntimeError('Worker 检测配置无效，统一安装已停止')


def install(config, values, stage, probe_needed):
    secret = config['secret']
    if probe_needed:
        if not AGENT_CONFIG.exists():
            private_write(AGENT_CONFIG, ''.join(f'{key}="{value}"\n' for key, value in values.items()))
        # The official installer reads config.conf; credentials never enter argv.
        run([str(stage / 'cf-probe'), 'install'], secret)
    run(['systemctl', 'enable', '--now', 'cf-probe.service'], secret)
    if (SYSTEMD_DIR / 'cfsm-route-detector.timer').exists():
        run(['systemctl', 'stop', 'cfsm-route-detector.timer', 'cfsm-route-detector.service'], secret)
    ROUTE_DIR.mkdir(parents=True, exist_ok=True)
    for name in ('route-detector.py', 'nexttrace'):
        shutil.copyfile(stage / name, ROUTE_DIR / name)
        (ROUTE_DIR / name).chmod(0o755)
    for name, target in (('route-detector.service', 'cfsm-route-detector.service'),
                         ('route-detector.timer', 'cfsm-route-detector.timer')):
        content = (stage / name).read_text()
        if name.endswith('.service'):
            content = content.replace('/usr/bin/python3', '"' + sys.executable + '"')
        (SYSTEMD_DIR / target).write_text(content)
        (SYSTEMD_DIR / target).chmod(0o644)
    private_write(ROUTE_CONFIG, json.dumps({**config, 'nexttrace': str(ROUTE_DIR / 'nexttrace')}))
    run(['systemctl', 'daemon-reload'], secret)
    run(['systemctl', 'enable', '--now', 'cfsm-route-detector.timer'], secret)
    print('监控服务已启动，正在执行首轮线路检测（最多约 15 分钟）…', flush=True)
    run(['systemctl', 'start', 'cfsm-route-detector.service'], secret, timeout=960)
    run(['systemctl', 'is-active', 'cf-probe.service'], secret)
    run(['systemctl', 'is-enabled', 'cfsm-route-detector.timer'], secret)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, help='首次安装时使用的私有 JSON 配置（权限 600）')
    parser.add_argument('--ipv6', action='store_true', help='同时检测 IPv4 与 IPv6')
    args = parser.parse_args()
    if platform.system() != 'Linux' or os.geteuid() != 0 or not Path('/run/systemd/system').is_dir():
        raise RuntimeError('统一安装入口需要 Linux、root 和正在运行的 systemd')
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    if not arch:
        raise RuntimeError('统一安装入口目前支持 x86_64 / aarch64 VPS')
    check_existing_probes()
    existing = AGENT_CONFIG.exists()
    probe_needed = not existing or not AGENT_BINARY.is_file()
    if existing and args.config:
        raise RuntimeError('已有监控配置会自动复用，请去掉 --config 后重新运行')
    values = read_agent_config(AGENT_CONFIG) if existing else {}
    if args.config:
        if args.config.stat().st_mode & 0o077:
            raise RuntimeError('安装配置必须为私有文件，请先 chmod 600')
        supplied = json.loads(args.config.read_text())
        values = {'SERVER_ID': supplied['id'], 'SECRET': supplied['secret'],
                  'WORKER_URL': supplied['url'], 'UA': supplied.get('ua', 'cfsm')}
    if not values:
        with open('/dev/tty', 'r+') as tty:
            for key, prompt in (('WORKER_URL', '监控站点地址（https://…）：'),
                                ('SERVER_ID', '后台已添加的服务器 ID：')):
                tty.write(prompt)
                tty.flush()
                values[key] = tty.readline().strip()
            values['SECRET'] = getpass.getpass('Agent 上报凭据（输入隐藏）：', stream=tty)
            tty.write('站点 Agent UA（直接回车使用 cfsm）：')
            tty.flush()
            values['UA'] = tty.readline().strip() or 'cfsm'
    if any(not isinstance(value, str) or any(char in value for char in '\r\n"') for value in values.values()):
        raise RuntimeError('配置值不能包含换行或双引号；凭据没有写入命令或日志')
    if not values.get('SERVER_ID'):
        raise RuntimeError('请先在后台添加服务器并取得 ID；统一安装入口不使用自动发现')
    print('已有监控配置将被复用。' if existing else '监控探针和线路检测将使用同一份站点配置。', flush=True)
    # /opt staging avoids the common /tmp noexec mount for downloaded binaries.
    with tempfile.TemporaryDirectory(prefix='cfsm-install-', dir='/opt') as folder:
        stage = Path(folder)
        for name in ('route-detector.py', 'route-detector.service', 'route-detector.timer'):
            download(f'{SOURCE}/{name}', stage / name)
        download(f'https://github.com/nxtrace/NTrace-core/releases/download/{NEXTTRACE_VERSION}/nexttrace_linux_{arch}',
                 stage / 'nexttrace', HASHES[arch][1])
        (stage / 'nexttrace').chmod(0o755)
        spec = importlib.util.spec_from_file_location('route_detector', stage / 'route-detector.py')
        detector = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(detector)
        url = values['WORKER_URL'].rstrip('/')
        if url.endswith('/update'):
            url = url[:-7]
        private_write(stage / 'config.json', json.dumps({'url': url, 'id': values['SERVER_ID'],
            'secret': values['SECRET'], 'nexttrace': str(stage / 'nexttrace'),
            'ua': values.get('UA') or 'cfsm',
            'families': [4, 6] if args.ipv6 else [4]}))
        config = detector.load_config(stage / 'config.json')
        check_monitor(detector, config)
        print('Worker 检测接口、节点 ID、上报凭据和数据库已通过检查。', flush=True)
        if probe_needed:
            download(f'https://github.com/huilang-me/cfsm-agent/releases/download/{AGENT_VERSION}/cf-probe-linux-{arch}',
                     stage / 'cf-probe', HASHES[arch][0])
            (stage / 'cf-probe').chmod(0o755)
        install(config, values, stage, probe_needed)
    print('安装完成：监控探针运行中，首轮线路结果已上报，以后每天检测一次。')
    print('请在节点网络详情查看识别结果；未完成或失败的路径会如实显示。')


if __name__ == '__main__':
    try:
        main()
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (ValueError, KeyError, OSError):
        print('安装未完成，请检查私有配置、文件权限及系统依赖；修正后可重新运行。', file=sys.stderr)
        sys.exit(1)
