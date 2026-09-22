# -*- coding: utf-8 -*-
"""通用 runner：运行目标脚本，把 stdout+stderr 落盘成 UTF-8 文件。
用法: python _run.py <target.py> <out.txt>
"""
import subprocess
import sys
from pathlib import Path

target = sys.argv[1]
out = Path(sys.argv[2])

p = subprocess.run([sys.executable, target],
                   capture_output=True, cwd=str(Path(target).resolve().parent.parent))

blob = (p.stdout or b"") + b"\n---STDERR---\n" + (p.stderr or b"")
out.write_bytes(blob if blob else b"(no output)")
print("done rc=%s" % p.returncode)
