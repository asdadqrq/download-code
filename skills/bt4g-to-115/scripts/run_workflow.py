#!/usr/bin/env python3
"""Run the workspace implementation; arguments are forwarded unchanged."""
import os
from pathlib import Path
import runpy
import sys

workspace=Path(os.environ.get('BT4G_WORKSPACE','/Users/fangdingding/PycharmProjects/PythonProject'))
entry=workspace/'bt4g_workflow.py'
if not entry.is_file():
    raise SystemExit('Workflow source missing; set BT4G_WORKSPACE to its workspace directory')
sys.path.insert(0,str(workspace))
runpy.run_path(str(entry),run_name='__main__')
