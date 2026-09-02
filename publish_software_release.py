from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import quote, urlsplit

try:
    import requests
except ImportError as exc:
    raise SystemExit("缺少 requests，请使用 E:\\ChosenSkin2.0\\Chosen\\.venv\\Scripts\\python.exe 运行") from exc

ROOT = Path(__file__).resolve().parent
CHOSEN_ROOT = ROOT.parent / "Chosen"
if str(CHOSEN_ROOT) not in sys.path:
    sys.path.insert(0, str(CHOSEN_ROOT))
try:
    from src.core.software_updater import validate_software_archive
except ImportError as exc:
    validate_software_archive = None
    PRODUCTION_VALIDATOR_ERROR = exc
else:
    PRODUCTION_VALIDATOR_ERROR = None

OWNER = "Chosen11111111"
REPOSITORY = "wogua"
R2_PUBLIC_BASE_URL = "https://cdn.chosen.cc.cd/wogua"
R2_OBJECT_PREFIX = "wogua"
PROXY_PREFIX = "https://v4.gh-proxy.org/"
BACKUP_PREFIXES = ("https://cdn.gh-proxy.org/", "https://v6.gh-proxy.org/")
CONTENT_RANGE_RE = re.compile(r"^bytes\s+(\d+)-(\d+)/(\d+)$")
DOWNLOAD_HEADERS = {"Accept": "*/*", "Accept-Encoding": "identity"}


class ReleaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class PackageInfo:
    version: str
    archive_path: Path
    size: int
    sha256: str

    @property
    def asset_name(self) -> str:
        return self.archive_path.name

    @property
    def tag(self) -> str:
        return f"v{self.version}"

    @property
    def github_url(self) -> str:
        return f"https://github.com/{OWNER}/{REPOSITORY}/releases/download/{self.tag}/{quote(self.asset_name, safe='')}"


def _read_version_payload(raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError("软件 ZIP 的 version.json 无法读取") from exc
    version = payload.get("version") if isinstance(payload, dict) else None
    if not isinstance(version, str) or not re.fullmatch(r"\d+(?:\.\d+)+", version.strip()):
        raise ReleaseError("软件 ZIP 的 version.json 版本无效")
    return version.strip()


def _read_archive_version(archive: zipfile.ZipFile) -> str:
    for name in ("_internal/version.json", "version.json"):
        try:
            return _read_version_payload(archive.read(name))
        except KeyError:
            continue
    raise ReleaseError("软件 ZIP 缺少 version.json")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_package(zip_path: Path | str) -> PackageInfo:
    path = Path(zip_path).expanduser().resolve()
    if not path.is_file() or path.suffix.casefold() != ".zip":
        raise ReleaseError("请选择一个存在的 ZIP 文件")
    try:
        with zipfile.ZipFile(path) as archive:
            version = _read_archive_version(archive)
            if archive.testzip() is not None:
                raise ReleaseError("软件 ZIP 存在损坏文件")
    except ReleaseError:
        raise
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReleaseError("文件不是有效的软件 ZIP") from exc
    if PRODUCTION_VALIDATOR_ERROR is not None or validate_software_archive is None:
        raise ReleaseError("无法加载 Chosen 生产 ZIP 校验器，请使用项目 Python 环境运行") from PRODUCTION_VALIDATOR_ERROR
    holder = tempfile.TemporaryDirectory(prefix="software-release-check-")
    try:
        validate_software_archive(path, Path(holder.name) / "extract", version)
    except Exception as exc:
        raise ReleaseError(f"生产 ZIP 校验失败: {exc}") from exc
    finally:
        holder.cleanup()
    return PackageInfo(version, path, path.stat().st_size, _sha256_file(path))


def r2_object_key(info: PackageInfo) -> str:
    return f"{R2_OBJECT_PREFIX}/{info.asset_name}"


def r2_public_url(info: PackageInfo) -> str:
    return f"{R2_PUBLIC_BASE_URL}/{quote(info.asset_name, safe='')}"


def build_manifest(info: PackageInfo) -> dict[str, object]:
    github_url = info.github_url
    return {
        "version": info.version,
        "download_url": r2_public_url(info),
        "download_url_backup": [
            PROXY_PREFIX + github_url,
            *[prefix + github_url for prefix in BACKUP_PREFIXES],
        ],
        "size": info.size,
        "sha256": info.sha256,
        "release_tag": info.tag,
    }


def _r2_client():
    required = ("R2_ENDPOINT", "R2_BUCKET", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY")
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        raise ReleaseError("缺少 R2 环境变量: " + ", ".join(missing))
    try:
        import boto3
    except ImportError as exc:
        raise ReleaseError("缺少 boto3，请在项目 Python 环境安装 boto3") from exc
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"].strip().rstrip("/"),
        region_name="auto",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"].strip(),
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"].strip(),
    )


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, Mapping):
        error = response.get("Error")
        if isinstance(error, Mapping):
            return str(error.get("Code", ""))
    return ""


def _is_missing_object(exc: Exception) -> bool:
    return _error_code(exc) in {"404", "NoSuchKey", "NotFound", "NoSuchObject"}


def upload_to_r2(info: PackageInfo, client=None) -> dict[str, object]:
    bucket = os.environ.get("R2_BUCKET", "").strip()
    if not bucket:
        raise ReleaseError("缺少 R2_BUCKET 环境变量")
    if client is None:
        client = _r2_client()
    key = r2_object_key(info)
    try:
        existing = client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        if not _is_missing_object(exc):
            code = _error_code(exc) or type(exc).__name__
            raise ReleaseError(f"检查 R2 对象失败: {code}") from exc
    else:
        try:
            existing_size = int(existing.get("ContentLength", -1))
        except (AttributeError, TypeError, ValueError) as exc:
            raise ReleaseError("R2 对象大小无效") from exc
        if existing_size != info.size:
            raise ReleaseError(f"R2 已存在同名但大小不同的对象: {key}")
        return {"uploaded": False, "bucket": bucket, "key": key, "object": existing}
    try:
        client.upload_file(
            str(info.archive_path),
            bucket,
            key,
            ExtraArgs={"ContentType": "application/zip"},
        )
        uploaded = client.head_object(Bucket=bucket, Key=key)
        uploaded_size = int(uploaded.get("ContentLength", -1))
    except Exception as exc:
        code = _error_code(exc) or type(exc).__name__
        raise ReleaseError(f"上传 R2 失败: {code}") from exc
    if uploaded_size != info.size:
        raise ReleaseError(f"上传后的 R2 对象大小不匹配: {key}")
    return {"uploaded": True, "bucket": bucket, "key": key, "object": uploaded}


def _validate_https_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise ReleaseError("下载地址无效")
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.scheme.casefold() != "https" or not parsed.netloc or parsed.username is not None or parsed.password is not None:
        raise ReleaseError("下载地址不安全")
    return value


def _close_response(response) -> None:
    if response is None:
        return
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def check_download_range(url: str, expected_size: int, http_get: Callable | None = None) -> bool:
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size <= 0:
        raise ReleaseError("期望的软件包大小无效")
    value = _validate_https_url(url)
    get = http_get or requests.get
    response = None
    try:
        response = get(
            value,
            headers={**DOWNLOAD_HEADERS, "Range": "bytes=0-0"},
            stream=True,
            allow_redirects=False,
            verify=True,
            timeout=(20, 60),
        )
        if getattr(response, "status_code", 0) != 206:
            raise ReleaseError(f"Range 测试状态码不是 206，而是 {getattr(response, 'status_code', 'unknown')}")
        headers = getattr(response, "headers", {}) or {}
        content_range = headers.get("Content-Range") or headers.get("content-range")
        match = CONTENT_RANGE_RE.fullmatch(str(content_range).strip())
        if match is None or tuple(map(int, match.groups())) != (0, 0, expected_size):
            raise ReleaseError(f"Range 测试返回范围不匹配: {content_range}")
        content_length = headers.get("Content-Length") or headers.get("content-length")
        if content_length is None or int(content_length) != 1:
            raise ReleaseError("Range 测试返回长度不是 1")
        return True
    except ReleaseError:
        raise
    except (OSError, requests.RequestException, TypeError, ValueError) as exc:
        raise ReleaseError(f"Range 测试失败: {exc}") from exc
    finally:
        _close_response(response)


def write_manifest(path: Path | str, payload: Mapping[str, object]) -> None:
    if not isinstance(payload, Mapping):
        raise ReleaseError("manifest 必须是对象")
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
    except (OSError, TypeError, ValueError) as exc:
        raise ReleaseError(f"写入 manifest 失败: {exc}") from exc
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def publish(zip_path: Path | str, manifest_path: Path | str | None = None, *, client=None, http_get: Callable | None = None) -> dict[str, object]:
    info = inspect_package(zip_path)
    upload_result = upload_to_r2(info, client=client)
    check_download_range(r2_public_url(info), info.size, http_get=http_get)
    manifest = build_manifest(info)
    if manifest_path is not None:
        write_manifest(manifest_path, manifest)
    return {"info": info, "upload": upload_result, "manifest": manifest}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="发布 Chosen 软件 ZIP 到 R2 并生成软件 manifest")
    parser.add_argument("zip_path", type=Path, help="Chosen 软件 ZIP 路径")
    parser.add_argument("--write-manifest", dest="manifest_path", type=Path, help="完成 R2 校验后写入 manifest 路径")
    args = parser.parse_args(argv)
    try:
        result = publish(args.zip_path, args.manifest_path)
    except ReleaseError as exc:
        print(f"发布失败: {exc}", file=sys.stderr)
        return 1
    info = result["info"]
    print(f"软件包校验通过: {info.asset_name} {info.version} {info.size} bytes")
    print(f"R2 地址: {r2_public_url(info)}")
    if args.manifest_path is not None:
        print(f"manifest 已写入: {args.manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
