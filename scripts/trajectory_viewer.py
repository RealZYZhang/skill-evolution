#!/usr/bin/env python3
"""Serve a read-only local browser for replay campaigns and trajectories."""

from __future__ import annotations

import argparse
from functools import partial
import html
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import sys
from typing import Any, Sequence
from urllib.parse import quote, unquote, urlsplit

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.trajectory_viewer_data import (
    ReplayRepository,
    TrajectoryUserReportRepository,
    ViewerDataError,
)
from scripts.skill_explorer_data import SkillExplorerRepository
from scripts.trajectory_profile_view import TrajectoryProfileViewRepository


APP_CSP = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "frame-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'none'"
)
ARTIFACT_CSP = (
    "sandbox; "
    "default-src 'none'; "
    "style-src 'unsafe-inline'; "
    "img-src data:; "
    "font-src data:; "
    "media-src data:; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


def _text_preview_document(file_path: Path, content: bytes) -> bytes:
    """Wrap plain text and Markdown in an inert, readable HTML preview."""

    try:
        source = content.decode("utf-8")
    except UnicodeDecodeError:
        source = content.decode("utf-8", errors="replace")
    title = html.escape(file_path.name)
    body = html.escape(source)
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · 只读预览</title>
<style>
:root {{
  color-scheme: light;
  font-family: "PingFang SC", "Microsoft YaHei", sans-serif;
}}
body {{ margin: 0; background: #f3f4ef; color: #17211d; }}
main {{
  width: min(920px, calc(100% - 2rem));
  margin: 1rem auto;
  padding: clamp(1rem, 3vw, 2.5rem);
  background: #fffefa;
  border: 1px solid #dfe2da;
  border-radius: 16px;
}}
h1 {{ margin: 0 0 .35rem; font-size: 1.25rem; overflow-wrap: anywhere; }}
p {{ margin: 0 0 1.25rem; color: #68736e; }}
pre {{
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font: 15px/1.75 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}}
</style>
</head>
<body>
<main><h1>{title}</h1><p>Markdown / 文本只读预览</p><pre>{body}</pre></main>
</body>
</html>
"""
    return document.encode("utf-8")


class ViewerRequestHandler(BaseHTTPRequestHandler):
    """Route read-only viewer requests to static assets and repository APIs."""

    protocol_version = "HTTP/1.1"
    server_version = "SkillEvolutionViewer/1"

    def __init__(
        self,
        *args: Any,
        repository: ReplayRepository,
        profile_repository: TrajectoryProfileViewRepository,
        report_repository: TrajectoryUserReportRepository,
        explorer_repository: SkillExplorerRepository,
        static_root: Path,
        **kwargs: Any,
    ) -> None:
        self.repository = repository
        self.profile_repository = profile_repository
        self.report_repository = report_repository
        self.explorer_repository = explorer_repository
        self.static_root = static_root
        super().__init__(*args, **kwargs)

    def do_GET(self) -> None:
        """Serve one GET request."""

        self._dispatch(head_only=False)

    def do_HEAD(self) -> None:
        """Serve one HEAD request with the same headers as GET."""

        self._dispatch(head_only=True)

    def do_POST(self) -> None:
        """Reject mutating methods."""

        self._method_not_allowed()

    def do_PUT(self) -> None:
        """Reject mutating methods."""

        self._method_not_allowed()

    def do_PATCH(self) -> None:
        """Reject mutating methods."""

        self._method_not_allowed()

    def do_DELETE(self) -> None:
        """Reject mutating methods."""

        self._method_not_allowed()

    def do_OPTIONS(self) -> None:
        """Reject cross-origin preflight and other unsupported methods."""

        self._method_not_allowed()

    def log_message(self, format: str, *args: Any) -> None:
        """Keep routine loopback requests out of the user's terminal."""

        return

    def log_error(self, format: str, *args: Any) -> None:
        """Keep unexpected server failures visible to the local operator."""

        BaseHTTPRequestHandler.log_message(self, format, *args)

    def _dispatch(self, *, head_only: bool) -> None:
        try:
            self._validate_host()
            raw_path = urlsplit(self.path).path
            path = unquote(raw_path)
            if path in STATIC_FILES:
                self._serve_static(path, head_only=head_only)
                return
            if path.startswith("/api/"):
                self._serve_api(path, head_only=head_only)
                return
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                "not_found",
                "The requested viewer resource was not found.",
                head_only=head_only,
            )
        except ViewerDataError as error:
            self._send_error_json(
                error.status,
                error.code,
                error.message,
                head_only=head_only,
            )
        except (OSError, UnicodeError) as error:
            self.log_error("Viewer I/O error: %s", error)
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "viewer_io_error",
                "The viewer could not read a preserved file.",
                head_only=head_only,
            )
        except Exception as error:
            self.log_error(
                "Unexpected viewer error: %s: %s",
                type(error).__name__,
                error,
            )
            self._send_error_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "viewer_internal_error",
                "The viewer encountered an unexpected error.",
                head_only=head_only,
            )

    def _validate_host(self) -> None:
        host_header = self.headers.get("Host")
        if not host_header:
            return
        try:
            hostname = urlsplit(f"//{host_header}").hostname
        except ValueError as error:
            raise ViewerDataError(
                "invalid_host",
                "The request Host header is invalid.",
            ) from error
        if hostname not in {"127.0.0.1", "localhost"}:
            raise ViewerDataError(
                "host_not_allowed",
                "The viewer only accepts local loopback requests.",
                403,
            )

    def _serve_static(self, path: str, *, head_only: bool) -> None:
        filename, content_type = STATIC_FILES[path]
        file_path = (self.static_root / filename).resolve()
        if (
            not file_path.is_relative_to(self.static_root.resolve())
            or not file_path.is_file()
        ):
            raise ViewerDataError(
                "static_file_missing",
                f"Viewer asset is missing: {filename}",
                500,
            )
        content = file_path.read_bytes()
        self._send_bytes(
            HTTPStatus.OK,
            content,
            content_type,
            head_only=head_only,
            headers={"Content-Security-Policy": APP_CSP},
        )

    def _serve_api(self, path: str, *, head_only: bool) -> None:
        parts = [part for part in path.strip("/").split("/") if part]
        if parts == ["api", "skills"]:
            self._send_json(
                HTTPStatus.OK,
                self.explorer_repository.list_skills(),
                head_only=head_only,
            )
            return
        if len(parts) == 3 and parts[:2] == ["api", "skills"]:
            self._send_json(
                HTTPStatus.OK,
                self.explorer_repository.get_skill(parts[2]),
                head_only=head_only,
            )
            return
        if (
            len(parts) == 4
            and parts[:2] == ["api", "skills"]
            and parts[3] == "revisions"
        ):
            self._send_json(
                HTTPStatus.OK,
                self.explorer_repository.list_revisions(parts[2]),
                head_only=head_only,
            )
            return
        if (
            len(parts) == 4
            and parts[:2] == ["api", "skills"]
            and parts[3] == "executions"
        ):
            self._send_json(
                HTTPStatus.OK,
                self.explorer_repository.list_executions(parts[2]),
                head_only=head_only,
            )
            return
        if (
            len(parts) == 5
            and parts[:2] == ["api", "skills"]
            and parts[3] == "executions"
        ):
            self._send_json(
                HTTPStatus.OK,
                self.explorer_repository.get_execution(parts[2], parts[4]),
                head_only=head_only,
            )
            return
        if (
            len(parts) == 4
            and parts[:2] == ["api", "skills"]
            and parts[3] == "improvements"
        ):
            self._send_json(
                HTTPStatus.OK,
                self.explorer_repository.list_improvements(parts[2]),
                head_only=head_only,
            )
            return
        if (
            len(parts) == 6
            and parts[:2] == ["api", "skills"]
            and parts[3] == "executions"
            and parts[5] == "analyses"
        ):
            self._send_json(
                HTTPStatus.OK,
                self.explorer_repository.get_execution_analyses(
                    parts[2], parts[4]
                ),
                head_only=head_only,
            )
            return
        if (
            len(parts) == 5
            and parts[:2] == ["api", "skills"]
            and parts[3:] == ["analyses", "multi"]
        ):
            self._send_json(
                HTTPStatus.OK,
                self.explorer_repository.list_multi_analyses(parts[2]),
                head_only=head_only,
            )
            return
        if (
            len(parts) == 6
            and parts[:2] == ["api", "skills"]
            and parts[3:5] == ["analyses", "multi"]
        ):
            self._send_json(
                HTTPStatus.OK,
                self.explorer_repository.get_multi_analysis(
                    parts[2], parts[5]
                ),
                head_only=head_only,
            )
            return
        if (
            len(parts) == 8
            and parts[:2] == ["api", "skills"]
            and parts[3] == "executions"
            and parts[5] == "files"
        ):
            self._serve_execution_file(parts, head_only=head_only)
            return
        if parts == ["api", "campaigns"]:
            if self.explorer_repository.has_hierarchy_data():
                self._send_json(
                    HTTPStatus.OK,
                    self.explorer_repository.list_campaign_projections(),
                    head_only=head_only,
                )
                return
            self._send_json(
                HTTPStatus.OK,
                self.repository.list_campaigns(),
                head_only=head_only,
            )
            return
        if len(parts) == 3 and parts[:2] == ["api", "campaigns"]:
            if self.explorer_repository.has_hierarchy_data():
                self._send_json(
                    HTTPStatus.OK,
                    self.explorer_repository.get_campaign_projection(parts[2]),
                    head_only=head_only,
                )
                return
            detail = self.repository.get_campaign(parts[2])
            self._send_json(
                HTTPStatus.OK,
                detail.to_dict(),
                head_only=head_only,
            )
            return
        if (
            len(parts) == 4
            and parts[:2] == ["api", "campaigns"]
            and parts[3] == "profile"
        ):
            if self.explorer_repository.has_hierarchy_data():
                self._send_json(
                    HTTPStatus.OK,
                    self.explorer_repository.get_campaign_profile_projection(
                        parts[2]
                    ),
                    head_only=head_only,
                )
                return
            self._send_json(
                HTTPStatus.OK,
                self.profile_repository.get_campaign_profile(parts[2]),
                head_only=head_only,
            )
            return
        if (
            len(parts) == 5
            and parts[:2] == ["api", "campaigns"]
            and parts[3] == "runs"
        ):
            if self.explorer_repository.has_hierarchy_data():
                self._send_json(
                    HTTPStatus.OK,
                    self.explorer_repository.get_campaign_run_projection(
                        parts[2], parts[4]
                    ),
                    head_only=head_only,
                )
                return
            detail = self.repository.get_run(parts[2], parts[4])
            self._send_json(
                HTTPStatus.OK,
                detail.to_dict(),
                head_only=head_only,
            )
            return
        if (
            len(parts) == 6
            and parts[:2] == ["api", "campaigns"]
            and parts[3] == "runs"
            and parts[5] == "analysis"
        ):
            if self.explorer_repository.has_hierarchy_data():
                self._send_json(
                    HTTPStatus.OK,
                    self.explorer_repository.get_campaign_analysis_projection(
                        parts[2], parts[4]
                    ),
                    head_only=head_only,
                )
                return
            self.repository.get_run(parts[2], parts[4])
            self._send_json(
                HTTPStatus.OK,
                self.report_repository.get_for_run(parts[4]),
                head_only=head_only,
            )
            return
        if (
            len(parts) == 7
            and parts[:2] == ["api", "campaigns"]
            and parts[3] == "runs"
        ):
            self._serve_run_file(parts, head_only=head_only)
            return
        self._send_error_json(
            HTTPStatus.NOT_FOUND,
            "api_route_not_found",
            "The requested viewer API route was not found.",
            head_only=head_only,
        )

    def _serve_execution_file(
        self,
        parts: list[str],
        *,
        head_only: bool,
    ) -> None:
        skill_id = parts[2]
        execution_id = parts[4]
        file_id = parts[6]
        operation = parts[7]
        file_path, declared_type = self.explorer_repository.get_execution_file(
            skill_id, execution_id, file_id
        )
        content_type = (
            declared_type
            or mimetypes.guess_type(file_path.name)[0]
            or "application/octet-stream"
        )
        content = file_path.read_bytes()
        headers: dict[str, str] = {}
        if operation == "preview":
            headers.update(
                {
                    "Content-Security-Policy": ARTIFACT_CSP,
                    "Cross-Origin-Resource-Policy": "same-origin",
                }
            )
            if content_type in {
                "text/markdown",
                "text/plain",
            } or file_path.suffix.lower() in {".md", ".markdown", ".txt"}:
                content = _text_preview_document(file_path, content)
                content_type = "text/html; charset=utf-8"
        elif operation == "download":
            encoded_name = quote(file_path.name)
            headers["Content-Disposition"] = (
                f"attachment; filename*=UTF-8''{encoded_name}"
            )
        else:
            self._send_error_json(
                HTTPStatus.NOT_FOUND,
                "execution_file_route_not_found",
                "The requested Execution file route was not found.",
                head_only=head_only,
            )
            return
        self._send_bytes(
            HTTPStatus.OK,
            content,
            content_type,
            head_only=head_only,
            headers=headers,
        )

    def _serve_run_file(
        self,
        parts: list[str],
        *,
        head_only: bool,
    ) -> None:
        campaign_id = parts[2]
        run_id = parts[4]
        kind = parts[5]
        operation = parts[6]
        if kind == "artifact" and operation == "preview":
            if self.explorer_repository.has_hierarchy_data():
                file_path, _ = (
                    self.explorer_repository.get_campaign_file_projection(
                        campaign_id, run_id, "artifact"
                    )
                )
            else:
                file_path = self.repository.get_run_file(
                    campaign_id,
                    run_id,
                    "artifact",
                )
            content = file_path.read_bytes()
            self._send_bytes(
                HTTPStatus.OK,
                content,
                "text/html; charset=utf-8",
                head_only=head_only,
                headers={
                    "Content-Security-Policy": ARTIFACT_CSP,
                    "Cross-Origin-Resource-Policy": "same-origin",
                },
            )
            return
        if operation == "download" and kind in {"artifact", "session"}:
            if self.explorer_repository.has_hierarchy_data():
                file_path, projected_type = (
                    self.explorer_repository.get_campaign_file_projection(
                        campaign_id, run_id, kind
                    )
                )
            else:
                file_path = self.repository.get_run_file(
                    campaign_id,
                    run_id,
                    kind,
                )
                projected_type = None
            content_type = (
                projected_type
                or (
                    "text/html; charset=utf-8"
                    if kind == "artifact"
                    else "application/x-ndjson; charset=utf-8"
                )
            )
            encoded_name = quote(file_path.name)
            self._send_bytes(
                HTTPStatus.OK,
                file_path.read_bytes(),
                content_type,
                head_only=head_only,
                headers={
                    "Content-Disposition": (
                        f"attachment; filename*=UTF-8''{encoded_name}"
                    )
                },
            )
            return
        self._send_error_json(
            HTTPStatus.NOT_FOUND,
            "run_file_route_not_found",
            "The requested run file route was not found.",
            head_only=head_only,
        )

    def _method_not_allowed(self) -> None:
        self._send_error_json(
            HTTPStatus.METHOD_NOT_ALLOWED,
            "method_not_allowed",
            "The viewer is read-only and supports only GET and HEAD.",
            head_only=False,
            headers={"Allow": "GET, HEAD"},
        )

    def _send_json(
        self,
        status: int | HTTPStatus,
        value: Any,
        *,
        head_only: bool,
    ) -> None:
        content = (
            json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        self._send_bytes(
            status,
            content,
            "application/json; charset=utf-8",
            head_only=head_only,
            headers={"Content-Security-Policy": "default-src 'none'"},
        )

    def _send_error_json(
        self,
        status: int | HTTPStatus,
        code: str,
        message: str,
        *,
        head_only: bool,
        headers: dict[str, str] | None = None,
    ) -> None:
        payload = {"error": {"code": code, "message": message}}
        content = (
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
        combined = {"Content-Security-Policy": "default-src 'none'"}
        combined.update(headers or {})
        self._send_bytes(
            status,
            content,
            "application/json; charset=utf-8",
            head_only=head_only,
            headers=combined,
        )

    def _send_bytes(
        self,
        status: int | HTTPStatus,
        content: bytes,
        content_type: str,
        *,
        head_only: bool,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(int(status))
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(content)


def create_server(
    replays_root: str | Path,
    port: int = 8765,
    harness_root: str | Path | None = None,
    analyses_root: str | Path | None = None,
    runtime_root: str | Path | None = None,
) -> ThreadingHTTPServer:
    """Create the loopback-only Skill Explorer without starting its loop."""

    if port < 0 or port > 65535:
        raise ValueError("port must be between 0 and 65535")
    repository = ReplayRepository(replays_root)
    resolved_replays_root = Path(replays_root).resolve()
    resolved_runtime_root = (
        Path(runtime_root).resolve()
        if runtime_root is not None
        else resolved_replays_root.parent
    )
    explorer_repository = SkillExplorerRepository(resolved_runtime_root)
    if not explorer_repository.has_hierarchy_data():
        repository.list_campaigns()
    profile_repository = TrajectoryProfileViewRepository(
        replays_root,
        harness_root,
    )
    report_repository = TrajectoryUserReportRepository(
        analyses_root
        if analyses_root is not None
        else resolved_replays_root.parent / "analyses"
    )
    static_root = (
        Path(__file__).resolve().parents[1] / "web" / "trajectory-viewer"
    )
    handler = partial(
        ViewerRequestHandler,
        repository=repository,
        profile_repository=profile_repository,
        report_repository=report_repository,
        explorer_repository=explorer_repository,
        static_root=static_root,
    )
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    return server


def _run_cli(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Serve the read-only Skill Explorer for Skills, Executions, "
            "Trajectories, and analyses."
        )
    )
    parser.add_argument(
        "--runtime-root",
        default=".skill-evolution",
        help="Skill Evolution runtime root.",
    )
    parser.add_argument(
        "--replays-root",
        default=".skill-evolution/replays",
        help="Directory containing replay campaign folders.",
    )
    parser.add_argument(
        "--analyses-root",
        default=".skill-evolution/analyses",
        help="Directory containing saved single-trajectory analysis AgentRuns.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Loopback port. Use 0 to select an available temporary port.",
    )
    options = parser.parse_args(arguments)
    try:
        server = create_server(
            options.replays_root,
            options.port,
            analyses_root=options.analyses_root,
            runtime_root=options.runtime_root,
        )
    except (OSError, ValueError, ViewerDataError) as error:
        parser.error(str(error))
    port = int(server.server_address[1])
    print(f"Skill Explorer: http://127.0.0.1:{port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nSkill Explorer stopped.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_cli())
