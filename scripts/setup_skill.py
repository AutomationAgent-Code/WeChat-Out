"""One-time local venv/dependency setup; no client, account or key access."""
import argparse
import ctypes
import os
import subprocess
import sys
import venv
from pathlib import Path


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    if sys.version_info < (3, 11):
        raise SystemExit('Python 3.11+ required')
    if os.name == 'nt' and ctypes.sizeof(ctypes.c_void_p) != 8:
        raise SystemExit('Use 64-bit Python for Windows memory capture; no automatic Python replacement')
    root = Path(__file__).resolve().parents[1]
    environment = root/'.venv'
    interpreter = environment/('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    if not environment.exists():
        venv.EnvBuilder(with_pip=True).create(environment)
    if not interpreter.is_file():
        raise SystemExit('Existing .venv is not valid; preserve it and choose a clean skill installation')
    subprocess.run([str(interpreter), '-m', 'pip', 'install', '-r', str(root/'requirements.txt')], check=True)
    print('Ready. Run this skill using its .venv Python. No WeChat data was accessed.')


if __name__ == '__main__':
    main()
