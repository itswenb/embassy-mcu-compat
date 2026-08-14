#!/usr/bin/env python3
"""按 sources.lock.toml 的提交锁定 Embassy 上游研究仓库。"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
import tomllib
from pathlib import Path


REPOSITORIES = {
    "embassy": ("https://github.com/embassy-rs/embassy.git", "embassy"),
    "stm32_data": ("https://github.com/embassy-rs/stm32-data.git", "stm32-data"),
    "stm32_data_generated": (
        "https://github.com/embassy-rs/stm32-data-generated.git",
        "stm32-data-generated",
    ),
    "chiptool": ("https://github.com/embassy-rs/chiptool.git", "chiptool"),
}
TARGET_DB = ("https://github.com/itswenb/cmsis-rust-target-db.git", "cmsis-rust-target-db")


def validate_revision(revision: str) -> None:
    if re.fullmatch(r"[0-9a-fA-F]{40}", revision) is None:
        raise ValueError(f"上游 revision 必须是 40 位 Git commit：{revision!r}")


def _run(*args: str, capture: bool = False) -> str:
    result = subprocess.run(
        args,
        check=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        text=True,
    )
    return result.stdout.strip() if capture else ""


def _normalized_url(url: str) -> str:
    return url.removesuffix(".git").rstrip("/")


def _retry_run(*args: str) -> None:
    for attempt in range(3):
        try:
            _run(*args)
            return
        except subprocess.CalledProcessError:
            if attempt == 2:
                raise
            time.sleep(2**attempt)


def parse_remote_head(output: str) -> str:
    fields = output.split()
    if len(fields) != 2 or fields[1] != "HEAD":
        raise ValueError("无法唯一解析滚动仓库 HEAD")
    validate_revision(fields[0])
    return fields[0].lower()


def remote_head(url: str) -> str:
    return parse_remote_head(_run("git", "ls-remote", url, "HEAD", capture=True))


def checkout(url: str, revision: str, target: Path) -> None:
    validate_revision(revision)
    created = not (target / ".git").is_dir()
    if created:
        target.parent.mkdir(parents=True, exist_ok=True)
        _retry_run("git", "clone", "--filter=blob:none", "--no-checkout", url, str(target))
    initial_checkout = created or not any(path.name != ".git" for path in target.iterdir())
    origin = _run("git", "-C", str(target), "remote", "get-url", "origin", capture=True)
    if _normalized_url(origin) != _normalized_url(url):
        raise ValueError(f"缓存仓库 origin 不匹配：{target} -> {origin}")
    if not initial_checkout and _run("git", "-C", str(target), "status", "--porcelain", capture=True):
        raise ValueError(f"缓存仓库存在未提交修改，拒绝切换：{target}")
    _retry_run("git", "-C", str(target), "fetch", "--depth=1", "origin", revision)
    _run("git", "-C", str(target), "checkout", "--detach", revision)
    actual = _run("git", "-C", str(target), "rev-parse", "HEAD", capture=True)
    if actual != revision:
        raise ValueError(f"上游 checkout 不匹配：期望 {revision}，实际 {actual}")


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, default=repo_root / "sources.lock.toml")
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=repo_root / ".cache/research/repos",
    )
    parser.add_argument("--include-target-db", action="store_true")
    args = parser.parse_args()
    lock = tomllib.loads(args.lock.read_text(encoding="utf-8"))
    upstream = lock["upstream"]
    for key, (url, directory) in REPOSITORIES.items():
        revision = str(upstream[key])
        checkout(url, revision, args.cache_dir / directory)
        print(f"已锁定 {directory}@{revision}")
    if args.include_target_db:
        url, directory = TARGET_DB
        revision = remote_head(url)
        checkout(url, revision, args.cache_dir / directory)
        print(f"已锁定 {directory}@{revision}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, KeyError, tomllib.TOMLDecodeError, subprocess.CalledProcessError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
