"""The sync daemon's local, single-writer inbox for Codex stream deltas."""

import json
import os
import queue
import socket
import socketserver
import threading

import codex_stream

SOCKET_PATH = os.path.join(os.environ.get("DSH_HOME") or
                           os.path.join(os.path.dirname(__file__), ".dsh-codex"),
                           "projection-writer.sock")
MAX_REQUEST_BYTES = 4 * 1024 * 1024


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if not line or len(line) > MAX_REQUEST_BYTES or not line.endswith(b"\n"):
            return
        try:
            request = json.loads(line)
            reply = queue.Queue(maxsize=1)
            self.server.inbox.put((request, reply))
            value = reply.get(timeout=60)
            self.wfile.write(json.dumps(value).encode("utf-8") + b"\n")
        except (ValueError, TypeError, queue.Empty, OSError):
            return


class _Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


class ProjectionWriter:
    def __init__(self, socket_path=SOCKET_PATH, append=None):
        self.socket_path = socket_path
        self.append = append or self._append
        self.inbox = queue.Queue()
        os.makedirs(os.path.dirname(socket_path), exist_ok=True)
        if os.path.exists(socket_path):
            os.unlink(socket_path)
        self.server = _Server(socket_path, _Handler)
        self.server.inbox = self.inbox
        os.chmod(socket_path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @staticmethod
    def _append(request):
        if not isinstance(request, dict):
            return None
        session_id = request.get("sessionId")
        chunks = request.get("chunks")
        if (not isinstance(session_id, str) or not session_id.startswith("session-")
                or not isinstance(chunks, list) or len(chunks) > 5000
                or any(not isinstance(chunk, dict) for chunk in chunks)):
            return None
        path = codex_stream.find_session_file(session_id)
        if path is None:
            return None
        return codex_stream._locked_append(
            path, codex_stream._chunk_events(chunks), fill_placement=True,
            min_turn=request.get("minTurn"), only_turn=request.get("onlyTurn"))

    def drain(self, timeout=0):
        """Perform one queued write on the sync daemon's main thread."""
        try:
            request, reply = self.inbox.get(timeout=timeout)
        except queue.Empty:
            return False
        try:
            result = self.append(request)
        except Exception:
            result = None
        reply.put(result)
        return True

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass


def send_chunks(session_id, chunks, min_turn=None, only_turn=None,
                socket_path=SOCKET_PATH):
    """Forward Codex deltas to the projector; the streamer never writes its log."""
    request = {"sessionId": session_id, "chunks": chunks,
               "minTurn": min_turn, "onlyTurn": only_turn}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(65)
            client.connect(socket_path)
            client.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
            data = bytearray()
            while len(data) <= MAX_REQUEST_BYTES:
                fragment = client.recv(65536)
                if not fragment:
                    break
                data.extend(fragment)
                if b"\n" in fragment:
                    return json.loads(data.split(b"\n", 1)[0])
    except (OSError, ValueError):
        pass
    return None
