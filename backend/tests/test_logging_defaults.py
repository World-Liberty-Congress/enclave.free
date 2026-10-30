import json
from pathlib import Path
import subprocess
import sys
import unittest


APP_DIR = Path(__file__).resolve().parents[1] / "app"


class LoggingDefaultsTest(unittest.TestCase):
    def test_info_default_with_either_import_order(self):
        # Ingest can initialize logging through a router import before main.
        # Execute both real configuration blocks in one fresh process: a later
        # basicConfig call does not replace an existing root handler.
        probe = """
import ast, io, json, logging, pathlib, sys
stream = io.StringIO()
sys.stderr = stream
for path in sys.argv[1:]:
    tree = ast.parse(pathlib.Path(path).read_text())
    for node in tree.body:
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
                and ast.unparse(node.value.func) == 'logging.basicConfig'):
            exec(compile(ast.Module(body=[node], type_ignores=[]), path, 'exec'))
logger = logging.getLogger('httpcore.connection')
logger.debug('dependency-debug-fixture')
app = logging.getLogger('enclave.main')
for level in [logging.INFO, logging.WARNING, logging.ERROR]:
    app.log(level, logging.getLevelName(level) + '-fixture')
print(json.dumps({'level': logging.getLogger().getEffectiveLevel(),
                  'messages': stream.getvalue()}))
"""
        for order in [("ingest.py", "main.py"), ("main.py", "ingest.py")]:
            with self.subTest(order=order):
                result = json.loads(subprocess.check_output(
                    [sys.executable, "-c", probe, *[str(APP_DIR / name) for name in order]],
                    text=True,
                ))
                self.assertEqual(result["level"], 20)
                self.assertNotIn("dependency-debug-fixture", result["messages"])
                for level in ["INFO", "WARNING", "ERROR"]:
                    self.assertIn(level + "-fixture", result["messages"])


if __name__ == "__main__":
    unittest.main()
