"""HTTP and CLI tests for the local read-only trajectory viewer."""

from __future__ import annotations

from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from typing import Iterator
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from scripts.trajectory_viewer import create_server
from tests.trajectory_viewer_fixtures import (
    create_campaign,
    create_trajectory_user_report,
)


@contextmanager
def running_viewer(replays_root: Path) -> Iterator[tuple[str, int]]:
    server = create_server(replays_root, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield host, int(port)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def request_json(base_url: str, path: str) -> dict[str, object]:
    with urlopen(f"{base_url}{path}", timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


class TrajectoryViewerHttpTest(unittest.TestCase):
    def test_static_assets_and_all_json_endpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replays_root = Path(temporary) / "replays"
            create_campaign(replays_root)
            create_trajectory_user_report(Path(temporary) / "analyses")
            with running_viewer(replays_root) as (host, port):
                base_url = f"http://{host}:{port}"
                with urlopen(base_url + "/", timeout=5) as response:
                    page = response.read().decode("utf-8")
                    self.assertEqual(response.status, 200)
                    self.assertIn("Skill 管理器", page)
                    self.assertIn("单 trajectory 分析", page)
                    self.assertIn(
                        "default-src 'self'",
                        response.headers["Content-Security-Policy"],
                    )
                with urlopen(
                    Request(base_url + "/app.js", method="HEAD"),
                    timeout=5,
                ) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), b"")
                    self.assertEqual(
                        response.headers["Cache-Control"],
                        "no-store",
                    )

                campaigns = request_json(base_url, "/api/campaigns")
                campaign = request_json(
                    base_url,
                    "/api/campaigns/campaign-1",
                )
                run = request_json(
                    base_url,
                    "/api/campaigns/campaign-1/runs/run-1",
                )
                profile = request_json(
                    base_url,
                    "/api/campaigns/campaign-1/profile",
                )
                analysis = request_json(
                    base_url,
                    "/api/campaigns/campaign-1/runs/run-1/analysis",
                )

                self.assertEqual(
                    campaigns["campaigns"][0]["campaign_id"],
                    "campaign-1",
                )
                self.assertEqual(campaign["summary"]["run_count"], 1)
                self.assertEqual(run["summary"]["record_count"], 12)
                self.assertEqual(
                    profile["schema"],
                    "trajectory.profile.v1",
                )
                self.assertNotIn(
                    "total_tokens",
                    profile["runs"][0]["resources"],
                )
                self.assertEqual(
                    analysis["report"]["analysis"]["status"],
                    "unavailable",
                )
                self.assertEqual(
                    analysis["report"]["schema"],
                    "analysis.single_trajectory_view.v1",
                )

    def test_missing_trajectory_analysis_is_visible_without_raw_fallback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replays_root = Path(temporary) / "replays"
            create_campaign(replays_root)
            with running_viewer(replays_root) as (host, port):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(
                        f"http://{host}:{port}/api/campaigns/"
                        "campaign-1/runs/run-1/analysis",
                        timeout=5,
                    )
                payload = json.loads(
                    raised.exception.read().decode("utf-8")
                )
                raised.exception.close()

        self.assertEqual(raised.exception.code, 404)
        self.assertEqual(
            payload["error"]["code"],
            "trajectory_analysis_not_found",
        )

    def test_artifact_preview_is_sandboxed_and_downloads_are_attachments(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replays_root = Path(temporary) / "replays"
            create_campaign(replays_root)
            with running_viewer(replays_root) as (host, port):
                base = (
                    f"http://{host}:{port}/api/campaigns/campaign-1"
                    "/runs/run-1"
                )
                with urlopen(
                    base + "/artifact/preview",
                    timeout=5,
                ) as response:
                    content = response.read().decode("utf-8")
                    csp = response.headers["Content-Security-Policy"]
                    self.assertIn("sandbox", csp)
                    self.assertIn("default-src 'none'", csp)
                    self.assertNotIn("script-src", csp)
                    self.assertIn("<script>", content)
                with urlopen(
                    base + "/artifact/download",
                    timeout=5,
                ) as response:
                    self.assertIn(
                        "attachment",
                        response.headers["Content-Disposition"],
                    )
                    self.assertIn(
                        "output.html",
                        response.headers["Content-Disposition"],
                    )
                with urlopen(
                    base + "/session/download",
                    timeout=5,
                ) as response:
                    self.assertIn(
                        "attachment",
                        response.headers["Content-Disposition"],
                    )
                    self.assertEqual(
                        response.headers.get_content_type(),
                        "application/x-ndjson",
                    )

    def test_mutating_methods_host_rebinding_and_traversal_are_rejected(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replays_root = Path(temporary) / "replays"
            create_campaign(replays_root)
            with running_viewer(replays_root) as (host, port):
                base_url = f"http://{host}:{port}"
                request = Request(
                    base_url + "/api/campaigns",
                    data=b"{}",
                    method="POST",
                )
                with self.assertRaises(HTTPError) as raised:
                    urlopen(request, timeout=5)
                self.assertEqual(raised.exception.code, 405)
                self.assertEqual(
                    raised.exception.headers["Allow"],
                    "GET, HEAD",
                )
                payload = json.loads(
                    raised.exception.read().decode("utf-8")
                )
                raised.exception.close()
                self.assertEqual(
                    payload["error"]["code"],
                    "method_not_allowed",
                )

                connection = http.client.HTTPConnection(host, port, timeout=5)
                connection.putrequest(
                    "GET",
                    "/api/campaigns",
                    skip_host=True,
                )
                connection.putheader("Host", "attacker.example")
                connection.endheaders()
                response = connection.getresponse()
                hostile_payload = json.loads(
                    response.read().decode("utf-8")
                )
                connection.close()
                self.assertEqual(response.status, 403)
                self.assertEqual(
                    hostile_payload["error"]["code"],
                    "host_not_allowed",
                )

                with self.assertRaises(HTTPError) as traversal:
                    urlopen(
                        base_url + "/api/campaigns/%2e%2e",
                        timeout=5,
                    )
                self.assertEqual(traversal.exception.code, 400)
                traversal_payload = json.loads(
                    traversal.exception.read().decode("utf-8")
                )
                traversal.exception.close()
                self.assertEqual(
                    traversal_payload["error"]["code"],
                    "invalid_campaign_id",
                )

    def test_requests_do_not_modify_preserved_campaign_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replays_root = Path(temporary) / "replays"
            campaign = create_campaign(replays_root)
            before = {
                str(path.relative_to(campaign)): (
                    path.read_bytes(),
                    path.stat().st_mtime_ns,
                )
                for path in campaign.rglob("*")
                if path.is_file()
            }
            with running_viewer(replays_root) as (host, port):
                base_url = f"http://{host}:{port}"
                request_json(base_url, "/api/campaigns")
                request_json(
                    base_url,
                    "/api/campaigns/campaign-1",
                )
                request_json(
                    base_url,
                    "/api/campaigns/campaign-1/runs/run-1",
                )
                with urlopen(
                    base_url
                    + "/api/campaigns/campaign-1/runs/run-1"
                    + "/artifact/preview",
                    timeout=5,
                ) as response:
                    response.read()
            after = {
                str(path.relative_to(campaign)): (
                    path.read_bytes(),
                    path.stat().st_mtime_ns,
                )
                for path in campaign.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)

    def test_error_responses_use_stable_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            replays_root = Path(temporary) / "replays"
            create_campaign(replays_root)
            with running_viewer(replays_root) as (host, port):
                with self.assertRaises(HTTPError) as raised:
                    urlopen(
                        f"http://{host}:{port}/api/not-a-route",
                        timeout=5,
                    )
                payload = json.loads(
                    raised.exception.read().decode("utf-8")
                )
                raised.exception.close()
                self.assertEqual(raised.exception.code, 404)
                self.assertEqual(
                    set(payload["error"]),
                    {"code", "message"},
                )

    def test_script_is_directly_invocable(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "scripts/trajectory_viewer.py",
                "--help",
            ],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--replays-root", result.stdout)
        self.assertIn("--analyses-root", result.stdout)
        self.assertIn("--port", result.stdout)


if __name__ == "__main__":
    unittest.main()
