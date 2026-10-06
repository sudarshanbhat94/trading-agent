"""Execute the actual legacy handlers without importing application startup.

Authentication itself is covered separately. Here guarded handler execution
must terminate before touching an engine, ledger, broker or background worker.
"""
import ast
import asyncio
from pathlib import Path
from typing import Any
import unittest

from fastapi import HTTPException, Request


class RetiredEntryControlsTest(unittest.TestCase):
    def handlers(self, guard):
        wanted={'start_agent','run_once','reset_demo','openclaw_run_cycle'}
        source=Path(__file__).resolve().parents[1]/'app/main.py'
        functions=[n for n in ast.parse(source.read_text()).body
                   if isinstance(n,ast.AsyncFunctionDef) and n.name in wanted]
        self.assertEqual({n.name for n in functions},wanted)
        for n in functions:n.decorator_list=[]
        module=ast.fix_missing_locations(ast.Module(body=functions,type_ignores=[]))
        context=dict(Any=Any,Request=Request,HTTPException=HTTPException,
                     require_user=guard,require_admin=guard,require_openclaw_bridge=guard,
                     settings=object(),db=object())
        exec(compile(module,str(source),'exec'),context)
        return {name:context[name] for name in wanted}

    def test_authorised_legacy_requests_are_retired_before_any_side_effect(self):
        calls=[]
        def guard(*args):calls.append(args);return {'id':1,'role':'admin'}
        for name,handler in self.handlers(guard).items():
            with self.subTest(handler=name),self.assertRaises(HTTPException) as raised:
                asyncio.run(handler(object()))
            self.assertEqual(raised.exception.status_code,410)
        self.assertEqual(len(calls),4)

    def test_unauthenticated_legacy_requests_cannot_bypass_the_guard(self):
        def guard(*args):raise HTTPException(401,'Authentication required')
        for name,handler in self.handlers(guard).items():
            with self.subTest(handler=name),self.assertRaises(HTTPException) as raised:
                asyncio.run(handler(object()))
            self.assertEqual(raised.exception.status_code,401)
