import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))
import docdrift as dd


class PlanTests(unittest.TestCase):
    def test_github_url_normalization_and_rejection(self):
        self.assertEqual(dd.normalize_github("https://github.com/acme/api/"), "https://github.com/acme/api.git")
        self.assertEqual(dd.normalize_github("https://github.com/acme/api.git"), "https://github.com/acme/api.git")
        with self.assertRaises(Exception):
            dd.normalize_github("https://example.com/acme/api")

    def test_scans_nested_readmes_but_skips_vendor_trees(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "README.md").write_text("# root")
            (root / "docs").mkdir()
            (root / "docs" / "README.md").write_text("# docs")
            (root / "node_modules" / "pkg").mkdir(parents=True)
            (root / "node_modules" / "pkg" / "README.md").write_text("# vendor")
            found = [p.relative_to(root).as_posix() for p in dd.find_readmes(root)]
        self.assertEqual(found, ["README.md", "docs/README.md"])

    def test_extracts_shell_steps_and_source_locations(self):
        source = """# Demo
## Install
```bash
$ python -m pip install -r requirements.txt
```
## Tests
```console
$ pytest -q
```
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "README.md"
            path.write_text(source)
            steps = dd.extract_steps(path)
        self.assertEqual([s.command for s in steps], ["python -m pip install -r requirements.txt", "pytest -q"])
        self.assertEqual([s.category for s in steps], ["install", "tests"])
        self.assertEqual(steps[1].line, 8)

    def test_extracts_inline_commands_outside_code_fences(self):
        source = """# README
## Quickstart
Run the server with `python src/app.py` and tests with `pytest -q`.
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "README.md"
            path.write_text(source)
            steps = dd.extract_steps(path)
        self.assertEqual([s.command for s in steps], ["python src/app.py", "pytest -q"])
        self.assertEqual([s.category for s in steps], ["quickstart", "tests"])

    def test_extracts_cd_chain_and_local_curl_only(self):
        source = """# Start
```sh
cd service && python app.py
curl -i http://127.0.0.1:8000/health
curl https://example.org/
```
"""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "README.md"
            path.write_text(source)
            steps = dd.extract_steps(path)
        self.assertEqual([s.command for s in steps], ["cd service && python app.py", "curl -i http://127.0.0.1:8000/health"])

    def test_conservative_repair_requires_a_unique_existing_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "src").mkdir()
            (root / "src" / "app.py").write_text("print('ok')")
            step = dd.Step("C001", "python app.py", "Quickstart", 4, "quickstart")
            self.assertEqual(dd.infer_repair(step, root), "python src/app.py")
            service = root / "service"
            (service / "src").mkdir(parents=True)
            (service / "src" / "worker.py").write_text("print('ok')")
            nested = dd.Step("C002", "cd service && python worker.py", "Quickstart", 5, "quickstart")
            self.assertEqual(dd.infer_repair(nested, root), "cd service && python src/worker.py")
            (root / "other").mkdir()
            (root / "other" / "app.py").write_text("print('ambiguous')")
            self.assertIsNone(dd.infer_repair(step, root))

    def test_endpoint_routes_from_readme(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "README.md"
            path.write_text("# API\nGET `/api/health` returns status.\n")
            claims = dd.endpoint_claims(path, "README.md")
        self.assertEqual(claims[0]["url"], "/api/health")
        self.assertEqual(claims[0]["file"], "README.md")

    def test_dangerous_patterns_are_marked(self):
        self.assertTrue(dd.DANGEROUS.search("sudo apt install x"))
        self.assertTrue(dd.DANGEROUS.search("rm -rf /"))
        self.assertFalse(dd.DANGEROUS.search("pytest -q"))

    def test_expected_json_response_is_compared_as_subset(self):
        self.assertTrue(dd.expected_response_matches('{"status":"ok","version":2}', '{"status":"ok"}'))
        self.assertFalse(dd.expected_response_matches('{"status":"error"}', '{"status":"ok"}'))

    def test_only_loopback_endpoint_is_probed(self):
        status, evidence = dd.try_endpoint_probe("unused", "https://example.com/", 1)
        self.assertEqual(status, "UNVERIFIABLE")
        self.assertIn("only localhost", evidence)

    def test_report_contains_machine_verdict_and_evidence(self):
        report = {
            "repository": "https://github.com/acme/api", "commit": "123abc", "created_at": "now", "readme": "README.md",
            "inventory": {"stacks": ["Python"], "test_files": ["tests/test_api.py"]},
            "claims": [{"id": "C001", "type": "tests", "claim": "pytest -q", "source": {"file": "README.md", "line": 8}, "status": "VERIFIED", "exit_code": 0, "duration_seconds": 1.2, "stdout": "2 passed", "stderr": ""}]
        }
        md = dd.render_markdown(report)
        self.assertIn("VERIFIED", md)
        self.assertIn("2 passed", md)
        self.assertIn("README.md:8", md)


if __name__ == "__main__":
    unittest.main()
