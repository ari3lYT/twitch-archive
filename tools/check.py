#!/usr/bin/env python3
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parents[1]
assert (root/'archiver/archive_schedule.py').read_bytes()==(root/'dashboard/archive_schedule.py').read_bytes(), 'Schedule copies differ'
for directory in ('archiver','dashboard'):
    subprocess.run([sys.executable,'-m','unittest','discover','-s',directory,'-v'],cwd=root,check=True)
