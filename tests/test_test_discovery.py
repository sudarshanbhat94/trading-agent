"""Prevent test methods being silently stranded after unittest.main()."""
import ast
from pathlib import Path
import unittest


class TestDiscoveryTest(unittest.TestCase):
    def test_test_functions_are_not_nested_in_module_control_flow(self):
        misplaced=[]
        for path in Path(__file__).parent.glob('test_*.py'):
            tree=ast.parse(path.read_text(),filename=str(path))
            for statement in tree.body:
                if isinstance(statement,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef)):
                    continue
                for node in ast.walk(statement):
                    if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name.startswith('test_'):
                        misplaced.append(f'{path.name}:{node.lineno}:{node.name}')
        self.assertEqual(misplaced,[], 'Tests outside discovery: '+', '.join(misplaced))
