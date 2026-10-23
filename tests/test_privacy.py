"""Guard: nothing private to the maintainers may ship in this repository.

Private words are stored as SHA-256 hashes (so this file does not itself contain them). Every word-like token in every
text file is hashed and compared; absolute home-directory paths and e-mail addresses are rejected too.
"""
import hashlib
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = {  # sha256(lowercase token)
    "f73137d930c31d188d901d57d78c13c88458d61600d1355d28c8841d590a6d69",
    "a8665b5b74113d5b72ae59e7866a0e092e8bd2a65fc23df126eb442f21c9f964",
    "b0474cdfe2bd090dfc66e4862a56720a332c5c723e3529001deb0b01abbb7a28",
    "034bfc5a1c16d7fa094bd823438870cc22deb6f9b991838a69af16231bdc6b72",
    "512a9b1e4ee99361428244abfe1e5cb31c21a1b26a81a318a93c925767c34895",
    "c25825a1239e67acc75aabee3263a9a301fcef07eb75f8ba06c63d6088eed35e",
    "3df913bcdc6186223fe97f4432d77c35c8c8d1fa3a2aba97c1d2b968148542cd",
}
PUBLISHER = "5708ada349f6d2cf2adee6c30aa0203d6761a42d067b3dab7be1fbf825b3fa74"  # allowed only in these files
PUBLISHER_FILES = {"LICENSE", "pyproject.toml", "README.md", "site/index.html", "docs/BUSINESS.md", "docs/LAUNCH.md"}
SKIP_DIRS = {".git", "__pycache__", "build", "dist", ".venv", "node_modules", "ftask.egg-info"}
HOME_PATH = re.compile(r"(?:/home/|/Users/)(?!you\b|user\b|name\b|me\b|\$)[A-Za-z0-9_.-]+")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@(?!example\.(?:com|org)\b)[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def text_files():
    for p in sorted(ROOT.rglob("*")):
        if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_DIRS) and not p.suffix in {".pyc", ".gif", ".png", ".db"}:
            try:
                yield p.relative_to(ROOT).as_posix(), p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue


class PrivacyTests(unittest.TestCase):
    def test_no_private_names_paths_or_emails(self):
        bad = []
        for name, text in text_files():
            for tok in set(re.findall(r"[a-z0-9]+", text.lower())):
                h = hashlib.sha256(tok.encode()).hexdigest()
                if h in PRIVATE or (h == PUBLISHER and name not in PUBLISHER_FILES):
                    bad.append(f"{name}: private word (sha256 {h[:8]})")
            for m in HOME_PATH.findall(text):
                bad.append(f"{name}: home path {m!r}")
            for m in EMAIL.findall(text):
                bad.append(f"{name}: e-mail address {m!r}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
