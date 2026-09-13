#!/usr/bin/env python3
"""Host-only reproducible image builder; Docker context works with a remote daemon."""
import hashlib
import io
import pathlib
import subprocess
import tarfile

HERE = pathlib.Path(__file__).resolve().parent

def source_id():
    names = ['go.mod'] + sorted(p.name for p in HERE.glob('*.go') if not p.name.endswith('_test.go'))
    digest = hashlib.sha256()
    for name in names:
        digest.update(name.encode() + b'\0' + (HERE / name).read_bytes() + b'\0')
    return digest.hexdigest()

if __name__ == '__main__':
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz', format=tarfile.USTAR_FORMAT) as tar:
        for name in ['Dockerfile', '.dockerignore', 'go.mod'] + sorted(p.name for p in HERE.glob('*.go')):
            tar.add(HERE / name, arcname=name)
    subprocess.run(['docker', 'build', '--build-arg', 'BUILD_ID=' + source_id(),
                    '-t', 'mesh-access-controller:dev', '-'], input=stream.getvalue(), check=True)
