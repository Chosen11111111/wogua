#!/usr/bin/env python3
"""从 ChosenreleaseNew/Chosen{ver} 目录打出 Chosen{ver}-full.zip 与 Chosen{ver}-app.zip。"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

APP_INTERNAL_DIRS = ("src", "ui", "assets", "injection", "Pengu Loader")


def _read_versions(release_dir: Path) -> tuple[str, str]:
    version_path = release_dir / "_internal" / "version.json"
    if not version_path.is_file():
        version_path = release_dir / "version.json"
    if not version_path.is_file():
        raise SystemExit(f"缺少 version.json: {release_dir}")
    payload = json.loads(version_path.read_text(encoding="utf-8-sig"))
    version = str(payload.get("version") or "").strip()
    runtime = str(payload.get("runtime_version") or "").strip()
    if not version:
        raise SystemExit("version.json 缺少 version")
    if not runtime:
        raise SystemExit(
            "version.json 缺少 runtime_version。"
            "请先写入例如 {\"version\":\"2.17\",\"runtime_version\":\"py314-pyside6-20260904\"}"
        )
    return version, runtime


def _add_file(zf: zipfile.ZipFile, source: Path, arcname: str) -> None:
    zf.write(source, arcname.replace("\\", "/"))


def _add_tree(zf: zipfile.ZipFile, root: Path, arc_prefix: str) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            _add_file(zf, path, f"{arc_prefix}/{rel}" if arc_prefix else rel)


def build_full_zip(release_dir: Path, out_zip: Path) -> None:
    required = (release_dir / "Chosen.exe", release_dir / "ChosenUpdater.exe", release_dir / "_internal")
    for path in required:
        if not path.exists():
            raise SystemExit(f"full 包缺少: {path}")
    if out_zip.exists():
        out_zip.unlink()
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        _add_file(zf, release_dir / "Chosen.exe", "Chosen.exe")
        _add_file(zf, release_dir / "ChosenUpdater.exe", "ChosenUpdater.exe")
        _add_tree(zf, release_dir / "_internal", "_internal")
        root_version = release_dir / "version.json"
        if root_version.is_file():
            _add_file(zf, root_version, "version.json")
    # 不把 Chosendata 打进更新 ZIP：客户端更新时会保留用户侧皮肤目录。


def build_app_zip(release_dir: Path, out_zip: Path) -> None:
    required = (release_dir / "Chosen.exe", release_dir / "ChosenUpdater.exe", release_dir / "_internal" / "version.json")
    for path in required:
        if not path.exists():
            raise SystemExit(f"app 包缺少: {path}")
    for name in APP_INTERNAL_DIRS:
        path = release_dir / "_internal" / name
        if not path.exists():
            raise SystemExit(f"app 包缺少白名单目录: {path}")
    if out_zip.exists():
        out_zip.unlink()
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        _add_file(zf, release_dir / "Chosen.exe", "Chosen.exe")
        _add_file(zf, release_dir / "ChosenUpdater.exe", "ChosenUpdater.exe")
        _add_file(zf, release_dir / "_internal" / "version.json", "_internal/version.json")
        for name in APP_INTERNAL_DIRS:
            _add_tree(zf, release_dir / "_internal" / name, f"_internal/{name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从发布目录生成 Chosen full/app 两个 ZIP")
    parser.add_argument(
        "release_dir",
        type=Path,
        help="例如 E:/ChosenSkin2.0/ChosenreleaseNew/Chosen2.17",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="ZIP 输出目录，默认写到 wogua 仓库根（脚本所在目录）",
    )
    args = parser.parse_args(argv)
    release_dir = args.release_dir.expanduser().resolve()
    if not release_dir.is_dir():
        raise SystemExit(f"发布目录不存在: {release_dir}")
    version, runtime = _read_versions(release_dir)
    out_dir = (args.out_dir or Path(__file__).resolve().parent).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    full_zip = out_dir / f"Chosen{version}-full.zip"
    app_zip = out_dir / f"Chosen{version}-app.zip"
    print(f"发布目录: {release_dir}")
    print(f"version={version} runtime_version={runtime}")
    print("正在生成 full ZIP（含完整 _internal 运行时）...")
    build_full_zip(release_dir, full_zip)
    print(f"full: {full_zip} ({full_zip.stat().st_size:,} bytes)")
    print("正在生成 app ZIP（仅业务层，不含 PySide6 等）...")
    build_app_zip(release_dir, app_zip)
    print(f"app:  {app_zip} ({app_zip.stat().st_size:,} bytes)")
    print("完成。接下来用 publish_software_release.bat 选择这两个 ZIP 上传。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
