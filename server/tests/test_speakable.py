import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from voice.speakable import code_refs, speakable  # noqa: E402


def test_paths_spoken_like_a_teammate():
    assert speakable("Login lives in app/api/auth/[auth0]/route.ts:18-24.") == "Login lives in the auth0 route file."
    assert speakable("See lib/specSheet.ts and demo_project/auth/login.py.") == "See the spec sheet file and the login file."
    assert speakable("Check the `server/session.py` file.") == "Check the session file."


def test_urls_and_versions_untouched():
    t = "We use Redis 7.2; see https://example.com/a/b."
    assert speakable(t) == t and code_refs(t) == []


def test_code_refs_keep_exact_paths():
    assert code_refs("app/api/auth/[auth0]/route.ts:18-24 and login.py") == ["app/api/auth/[auth0]/route.ts", "login.py"]
