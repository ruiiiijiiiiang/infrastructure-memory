import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infrastructure_memory.config import Config


class RuntimeDirectoryTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.filename = self.root / "config.json"
        self.filename.write_text("{}")

    def tearDown(self):
        self.temporary.cleanup()

    def test_missing_runtime_directory_uses_current_user_runtime_path(self):
        with (
            patch.dict(os.environ),
            patch("infrastructure_memory.config.os.getuid", return_value=4242),
        ):
            os.environ.pop("XDG_RUNTIME_DIR", None)
            os.environ["XDG_STATE_HOME"] = str(self.root / "state")
            config = Config.load(self.filename)
        self.assertEqual(
            Path("/run/user/4242/infrastructure-memory/service.sock"), config.socket
        )

    def test_empty_runtime_directory_uses_current_user_runtime_path(self):
        with (
            patch.dict(os.environ, {"XDG_RUNTIME_DIR": ""}),
            patch("infrastructure_memory.config.os.getuid", return_value=4242),
        ):
            config = Config.load(self.filename)
        self.assertEqual(
            Path("/run/user/4242/infrastructure-memory/service.sock"), config.socket
        )

    def test_explicit_runtime_directory_is_preserved(self):
        runtime = self.root / "custom-runtime"
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
            config = Config.load(self.filename)
        self.assertEqual(runtime / "infrastructure-memory/service.sock", config.socket)

    def test_explicit_socket_configuration_is_preserved(self):
        socket = self.root / "custom-socket/service.sock"
        self.filename.write_text('{"socket": "' + str(socket) + '"}')
        with patch.dict(os.environ):
            os.environ.pop("XDG_RUNTIME_DIR", None)
            config = Config.load(self.filename)
        self.assertEqual(socket, config.socket)

    def test_service_and_reduced_mcp_environment_resolve_same_socket(self):
        with (
            patch.dict(os.environ),
            patch("infrastructure_memory.config.os.getuid", return_value=4242),
        ):
            os.environ["XDG_RUNTIME_DIR"] = "/run/user/4242"
            service = Config.load(self.filename)
            os.environ.pop("XDG_RUNTIME_DIR")
            mcp = Config.load(self.filename)
        self.assertEqual(service.socket, mcp.socket)


if __name__ == "__main__":
    unittest.main()
