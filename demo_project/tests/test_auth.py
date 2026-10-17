"""Offline proof that the current login path issues a JWT."""

import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from auth.jwt_tokens import verify_token
from auth.login import LoginError, login, password_digest
from db.models import User


class LoginTests(unittest.TestCase):
    def setUp(self):
        salt = b"fictional-demo-salt"
        self.user = User("user-1", "demo@example.test", salt,
                         password_digest("correct horse", salt))
        self.find_user = lambda email: self.user if email == self.user.email else None

    def test_login_issues_signed_jwt(self):
        token = login(self.user.email, "correct horse", self.find_user, "demo-secret")
        claims = verify_token(token, "demo-secret")
        self.assertEqual(claims["sub"], "user-1")
        self.assertIn("invoices:write", claims["scopes"])

    def test_wrong_password_is_rejected(self):
        with self.assertRaises(LoginError):
            login(self.user.email, "wrong", self.find_user, "demo-secret")
