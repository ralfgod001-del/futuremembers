"""Add tests for load_env_file + DEEPSEEK_MODEL env override."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from futures_positions.reports import (
    DEEPSEEK_DEFAULT_MODEL,
    call_deepseek,
    load_env_file,
)


class LoadEnvFileTest(unittest.TestCase):
    def test_parses_key_value_pairs(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text(
                "# comment\n"
                "DEEPSEEK_API_KEY=sk-test\n"
                "DEEPSEEK_MODEL=deepseek-v4-pro\n"
                "QUOTED='with quotes'\n"
                'DOUBLE="double"\n'
                "\n"
                "INVALID LINE WITHOUT EQ\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=False):
                # remove keys to test setdefault behavior
                for k in ("DEEPSEEK_API_KEY", "DEEPSEEK_MODEL", "QUOTED", "DOUBLE"):
                    os.environ.pop(k, None)
                loaded = load_env_file(path, override=True)
            self.assertEqual(loaded["DEEPSEEK_API_KEY"], "sk-test")
            self.assertEqual(loaded["DEEPSEEK_MODEL"], "deepseek-v4-pro")
            self.assertEqual(loaded["QUOTED"], "with quotes")
            self.assertEqual(loaded["DOUBLE"], "double")
            self.assertNotIn("INVALID LINE WITHOUT EQ", loaded)

    def test_missing_file_returns_empty(self):
        result = load_env_file(Path("C:/nonexistent/.env"))
        self.assertEqual(result, {})

    def test_does_not_override_existing_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("MY_TEST_KEY=from_file", encoding="utf-8")
            with patch.dict(os.environ, {"MY_TEST_KEY": "from_env"}, clear=False):
                loaded = load_env_file(path)
                # We loaded it from file but env wins.
                self.assertEqual(loaded["MY_TEST_KEY"], "from_file")
                self.assertEqual(os.environ["MY_TEST_KEY"], "from_env")
            os.environ.pop("MY_TEST_KEY", None)

    def test_override_flag_replaces_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("MY_TEST_KEY_OVERRIDE=from_file", encoding="utf-8")
            with patch.dict(os.environ, {"MY_TEST_KEY_OVERRIDE": "from_env"}, clear=False):
                load_env_file(path, override=True)
                self.assertEqual(os.environ["MY_TEST_KEY_OVERRIDE"], "from_file")
            os.environ.pop("MY_TEST_KEY_OVERRIDE", None)


class CallDeepseekModelOverrideTest(unittest.TestCase):
    def test_explicit_model_wins_over_env(self):
        fake = MagicMock(); fake.json.return_value = {"choices": [{"message": {"content": "x"}}]}
        fake.raise_for_status.return_value = None
        sess = MagicMock(); sess.post.return_value = fake
        with patch.dict(os.environ, {"DEEPSEEK_MODEL": "from-env"}, clear=False):
            call_deepseek("hi", api_key="k", model="from-arg", http=sess)
        payload = __import__("json").loads(sess.post.call_args.kwargs["data"].decode("utf-8"))
        self.assertEqual(payload["model"], "from-arg")

    def test_env_model_used_when_arg_none(self):
        fake = MagicMock(); fake.json.return_value = {"choices": [{"message": {"content": "x"}}]}
        fake.raise_for_status.return_value = None
        sess = MagicMock(); sess.post.return_value = fake
        with patch.dict(os.environ, {"DEEPSEEK_MODEL": "from-env"}, clear=False):
            call_deepseek("hi", api_key="k", model=None, http=sess)
        payload = __import__("json").loads(sess.post.call_args.kwargs["data"].decode("utf-8"))
        self.assertEqual(payload["model"], "from-env")

    def test_default_model_when_neither_arg_nor_env(self):
        fake = MagicMock(); fake.json.return_value = {"choices": [{"message": {"content": "x"}}]}
        fake.raise_for_status.return_value = None
        sess = MagicMock(); sess.post.return_value = fake
        # Ensure env doesn't leak
        with patch.dict(os.environ, {}, clear=True):
            call_deepseek("hi", api_key="k", model=None, http=sess)
        payload = __import__("json").loads(sess.post.call_args.kwargs["data"].decode("utf-8"))
        self.assertEqual(payload["model"], DEEPSEEK_DEFAULT_MODEL)

    def test_response_format_omitted_for_reasoning_models(self):
        fake = MagicMock(); fake.json.return_value = {"choices": [{"message": {"content": "x"}}]}
        fake.raise_for_status.return_value = None
        sess = MagicMock(); sess.post.return_value = fake
        call_deepseek("hi", api_key="k", model="deepseek-v4-pro", http=sess)
        payload = __import__("json").loads(sess.post.call_args.kwargs["data"].decode("utf-8"))
        self.assertNotIn("response_format", payload)

    def test_max_tokens_param_forwarded(self):
        fake = MagicMock(); fake.json.return_value = {"choices": [{"message": {"content": "x"}}]}
        fake.raise_for_status.return_value = None
        sess = MagicMock(); sess.post.return_value = fake
        call_deepseek("hi", api_key="k", model="deepseek-v4-pro", max_tokens=9999, http=sess)
        payload = __import__("json").loads(sess.post.call_args.kwargs["data"].decode("utf-8"))
        self.assertEqual(payload["max_tokens"], 9999)


if __name__ == "__main__":
    unittest.main()
