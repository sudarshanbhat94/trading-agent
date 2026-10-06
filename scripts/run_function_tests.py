#!/usr/bin/env python3
"""Run pytest function fixtures that unittest discovery does not execute."""
import ast
from pathlib import Path
import pytest


def main():
    selected=[]
    root=Path(__file__).resolve().parents[1]
    for path in sorted((root/'tests').glob('test_*.py')):
        for node in ast.parse(path.read_text()).body:
            if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith('test_'):
                selected.append(str(path)+'::'+node.name)
    if not selected:
        raise RuntimeError('No research/UI function tests discovered')
    return pytest.main(['-q',*selected])


if __name__=='__main__':
    raise SystemExit(main())
