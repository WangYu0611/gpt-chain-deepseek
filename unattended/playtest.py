"""Prepare and open an isolated copy of the latest accepted local game."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile

from github_harness_lab.lab_harness.process import run_process, safe_environment
from .config import load_config
from .storage import lock, read_json, write_json

ROOT = Path(__file__).resolve().parent.parent
ENGINE = Path('C:/Program Files/Epic Games/UE_5.8/Engine')


def git(work: Path, *arguments: str) -> str:
    return subprocess.check_output(['git', '-C', str(work), *arguments], text=True, encoding='utf-8').strip()


def prepare(config_path: Path) -> dict:
    config = load_config(config_path)
    runtime = Path(config['runtime_dir'])
    work = runtime / 'workspace'
    state = read_json(runtime / 'state.json')
    accepted = state.get('accepted', [])
    if not accepted:
        raise RuntimeError('No accepted task is available for playtesting.')
    latest = accepted[-1]
    sha = latest['head_sha']
    if not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise RuntimeError('A complete accepted commit is required.')
    base = runtime / 'playtest'
    base.mkdir(exist_ok=True)
    folder = base / (latest['id'] + '-' + sha[:12])
    with lock(base / 'prepare.lock'):
        manifest = folder / 'playtest.json'
        if manifest.exists():
            existing = read_json(manifest)
            if existing['head_sha'] != sha or not Path(existing['project']).is_file():
                raise RuntimeError('Playtest metadata does not match the accepted commit.')
            write_json(base / 'current.json', existing)
            return existing
        source = folder / 'project'
        source.mkdir(parents=True, exist_ok=True)
        archive = folder / 'source.tar'
        if not archive.exists():
            subprocess.run(['git', '-C', str(work), 'archive', '--format=tar', '--output=' + str(archive), sha,
                            'AGENTS.md', '.gitattributes', '.gitignore', 'Config', 'Content', 'Source',
                            'ZhuGuoCity.uproject'], check=True)
        with tarfile.open(archive) as tar:
            for member in tar.getmembers():
                relative = PurePosixPath(member.name)
                if relative.is_absolute() or '..' in relative.parts or not (member.isfile() or member.isdir()):
                    raise RuntimeError('Unsafe source archive entry.')
            tar.extractall(source, filter='data')
        common = Path(git(work, 'rev-parse', '--git-common-dir'))
        if not common.is_absolute():
            common = work / common
        for asset in (source / 'Content').rglob('*'):
            if not asset.is_file() or asset.stat().st_size > 1024:
                continue
            raw = asset.read_bytes()
            if not raw.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
                continue
            pointer = raw.decode('ascii')
            oid = re.search(r'^oid sha256:([0-9a-f]{64})$', pointer, re.M)
            size = re.search(r'^size ([0-9]+)$', pointer, re.M)
            if not oid or not size:
                raise RuntimeError('Invalid LFS asset pointer: ' + asset.name)
            digest = oid.group(1)
            payload = (common / 'lfs/objects' / digest[:2] / digest[2:4] / digest).read_bytes()
            if len(payload) != int(size.group(1)) or hashlib.sha256(payload).hexdigest() != digest:
                raise RuntimeError('Local LFS asset does not match the accepted commit.')
            asset.write_bytes(payload)
        project = source / 'ZhuGuoCity.uproject'
        build_dir = folder / ('build-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
        argv = [str(ENGINE / 'Binaries/ThirdParty/DotNet/10.0/win-x64/dotnet.exe'),
                str(ENGINE / 'Binaries/DotNET/UnrealBuildTool/UnrealBuildTool.dll'),
                'ZhuGuoCityEditor', 'Win64', 'Development', '-project=' + str(project),
                '-waitmutex', '-NoHotReloadFromIDE', '-NoUBTMakefiles', '-ForceHeaderGeneration',
                '-log=' + str(folder / 'UnrealBuildTool.log')]
        print('Preparing latest accepted playtest: ' + latest['id'] + ' / ' + sha[:12], flush=True)
        result = run_process(argv, cwd=source, output_dir=build_dir, timeout_seconds=1800,
                             environment=safe_environment(), max_output_bytes=32 * 1024 * 1024)
        output = result.stdout_path.read_text(encoding='utf-8', errors='replace')
        if result.exit_code or result.timed_out or result.output_limit_exceeded or 'Result: Succeeded' not in output:
            raise RuntimeError('Playtest build failed; see ' + str(build_dir))
        record = {'task_id': latest['id'], 'title': latest['title'], 'head_sha': sha,
                  'project': str(project), 'build_stdout': str(result.stdout_path),
                  'built_at': datetime.now(timezone.utc).isoformat(),
                  'editor': str(ENGINE / 'Binaries/Win64/UnrealEditor.exe')}
        write_json(manifest, record)
        write_json(base / 'current.json', record)
        return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT / 'unattended.local.json')
    parser.add_argument('--prepare-only', action='store_true')
    args = parser.parse_args()
    record = prepare(args.config)
    if not args.prepare_only:
        argv = [record['editor'], record['project'], '-game', '-windowed', '-ResX=1600', '-ResY=900',
                '-nosplash', '-DisablePlugins=Fab']
        process = subprocess.Popen(argv, cwd=str(Path(record['project']).parent),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, close_fds=True)
        print('Opened ' + record['task_id'] + ', PID=' + str(process.pid), flush=True)
    print(json.dumps(record, ensure_ascii=True), flush=True)


if __name__ == '__main__':
    main()
