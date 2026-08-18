"""HTTP tests for Skill-first routes and artifact security boundaries."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import threading
from typing import Iterator
import unittest
from urllib.request import urlopen

from scripts.trajectory_viewer import create_server
from tests.test_skill_explorer_data import _write_execution, _write_skill


@contextmanager
def _running(runtime: Path) -> Iterator[str]:
    server = create_server(
        runtime / "replays",
        port=0,
        runtime_root=runtime,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _json(base: str, path: str) -> dict[str, object]:
    with urlopen(base + path, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


class SkillExplorerHttpTests(unittest.TestCase):
    """Serve the complete Skill-to-Execution read-only navigation."""

    def test_skill_routes_and_campaign_projection_read_same_objects(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = root / "runtime"
            _write_execution(runtime, _write_skill(root / "packages"))

            with _running(runtime) as base:
                skills = _json(base, "/api/skills")
                skill = _json(base, "/api/skills/viewer-skill")
                revisions = _json(base, "/api/skills/viewer-skill/revisions")
                executions = _json(base, "/api/skills/viewer-skill/executions")
                execution = _json(
                    base,
                    "/api/skills/viewer-skill/executions/execution-1",
                )
                analyses = _json(
                    base,
                    "/api/skills/viewer-skill/executions/execution-1/analyses",
                )
                multi = _json(
                    base, "/api/skills/viewer-skill/analyses/multi"
                )
                improvements = _json(
                    base, "/api/skills/viewer-skill/improvements"
                )
                campaigns = _json(base, "/api/campaigns")

                self.assertEqual(skills["skills"][0]["skill_id"], "viewer-skill")
                self.assertEqual(skill["package"]["contract"]["status"], "approved")
                self.assertEqual(len(revisions["revisions"]), 1)
                self.assertEqual(executions["executions"][0]["execution_id"], "execution-1")
                self.assertEqual(execution["input"]["artifacts"][0]["artifact_id"], "input-1")
                self.assertEqual(analyses["analyses"], [])
                self.assertEqual(multi["analyses"], [])
                self.assertEqual(improvements["candidates"], [])
                self.assertEqual(campaigns["campaigns"][0]["run_count"], 1)

                with urlopen(
                    base
                    + "/api/skills/viewer-skill/executions/execution-1/"
                    "files/output-1/preview",
                    timeout=5,
                ) as response:
                    self.assertIn(
                        "sandbox", response.headers["Content-Security-Policy"]
                    )
                    self.assertEqual(response.headers.get_content_type(), "text/html")

                with urlopen(
                    base
                    + "/api/skills/viewer-skill/executions/execution-1/"
                    "files/input-1/preview",
                    timeout=5,
                ) as response:
                    content = response.read().decode("utf-8")
                    self.assertEqual(
                        response.headers.get_content_type(), "text/html"
                    )
                    self.assertIn("Markdown / 文本只读预览", content)
                    self.assertIn("input", content)
                    self.assertIn(
                        "sandbox", response.headers["Content-Security-Policy"]
                    )


if __name__ == "__main__":
    unittest.main()
