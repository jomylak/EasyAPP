"""Loopback file server for handing local documents to Skyvern.

Skyvern uploads files to a form by *downloading* them from a URL first
(``handle_upload_file_action`` -> ``handler_utils.download_file``). Local
filesystem paths are accepted only inside Skyvern's own per-run download
directory, whose id isn't known until the run has already started -- so the
tailored resume can't simply be handed over as a path.

Rather than publish the resume to S3 or a presigned URL (it is a personal
document and should never leave the machine), we serve it over loopback and
give Skyvern a ``http://127.0.0.1:<port>/<file>`` URL. Skyvern runs on the same
host, so the fetch never touches the network.

Two things matter for this to work:

- Use the literal IP ``127.0.0.1``. Skyvern's SSRF guard ships with
  ``BLOCKED_HOSTS = ["localhost"]``, so the *hostname* is rejected.
- Skyvern must have ``ALLOWED_HOSTS=["127.0.0.1"]`` set. An entry there
  bypasses the blocked-host, internal-hostname and private-IP checks in
  ``skyvern/utils/url_validators.py``; without it the fetch fails with
  ``BlockedHost`` and the resume silently never uploads.
"""

import contextlib
import logging
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logger = logging.getLogger(__name__)

# Loopback only. Never bind 0.0.0.0 -- this serves personal documents.
BIND_HOST = "127.0.0.1"


class _QuietHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that logs to the logger instead of stderr.

    The default implementation writes every request to stderr, which would
    corrupt the Rich live dashboard.
    """

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logger.debug("[fileserver] %s", format % args)


class DocumentServer:
    """Serves one directory over loopback for the lifetime of a job.

    Use as a context manager so the socket is always released, even when the
    application fails partway through::

        with DocumentServer(upload_dir, worker_id=0) as srv:
            url = srv.url_for("Jane_Doe_Resume.pdf")
    """

    def __init__(self, directory: Path, worker_id: int = 0, port_base: int = 8100):
        self.directory = Path(directory)
        self.worker_id = worker_id
        self.port = port_base + worker_id
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> "DocumentServer":
        """Bind and serve in a daemon thread."""
        if not self.directory.is_dir():
            raise FileNotFoundError(f"Document directory does not exist: {self.directory}")

        handler = partial(_QuietHandler, directory=str(self.directory))
        # allow_reuse_address avoids TIME_WAIT collisions between consecutive
        # jobs on the same worker.
        ThreadingHTTPServer.allow_reuse_address = True
        try:
            self._server = ThreadingHTTPServer((BIND_HOST, self.port), handler)
        except OSError as exc:
            raise OSError(
                f"Could not bind document server on {BIND_HOST}:{self.port} "
                f"for worker {self.worker_id}: {exc}"
            ) from exc

        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"fileserver-{self.worker_id}",
            daemon=True,
        )
        self._thread.start()
        logger.info("[worker-%d] Serving %s at %s",
                    self.worker_id, self.directory, self.base_url)
        return self

    def stop(self) -> None:
        """Shut the server down and release the port."""
        if self._server is not None:
            with contextlib.suppress(Exception):
                self._server.shutdown()
            with contextlib.suppress(Exception):
                self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def base_url(self) -> str:
        return f"http://{BIND_HOST}:{self.port}"

    def url_for(self, filename: str) -> str:
        """Build the fetch URL for a file in the served directory.

        Args:
            filename: Bare filename (not a path) inside the served directory.
        """
        from urllib.parse import quote
        return f"{self.base_url}/{quote(filename)}"

    def __enter__(self) -> "DocumentServer":
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()
