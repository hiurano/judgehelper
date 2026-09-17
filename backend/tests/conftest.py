"""Test isolation: never import the application against the working database."""
import os
import shutil
import tempfile


_test_dir = tempfile.mkdtemp(prefix="judge-helper-tests-")
os.environ["DB_PATH"] = os.path.join(_test_dir, "tests.db")
os.environ["LOGS_DIR"] = os.path.join(_test_dir, "logs")
os.environ["AUTH_USERNAME"] = "test"
os.environ["AUTH_PASSWORD"] = "Test-Password-2026"
os.environ["SECRET_KEY"] = "test-only-secret-key-with-at-least-32-characters"


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_test_dir, ignore_errors=True)
