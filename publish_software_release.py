from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from tkinter import END, BOTH, LEFT, X, Button, Entry, Frame, Label, StringVar, Tk, filedialog, messagebox
from tkinter.scrolledtext import ScrolledText
from typing import Callable, Mapping
from urllib.parse import quote, urlsplit

try:
    import requests
except ImportError as exc:
    raise SystemExit("缺少 requests，请使用 E:\\ChosenSkin2.0\\Chosen\\.venv\\Scripts\\python.exe 运行") from exc

try:
    import truststore
except ImportError:
    truststore = None

ROOT = Path(__file__).resolve().parent
CHOSEN_ROOT = ROOT.parent / "Chosen"
if str(CHOSEN_ROOT) not in sys.path:
    sys.path.insert(0, str(CHOSEN_ROOT))
try:
    from src.core.software_updater import validate_software_archive, validate_software_manifest
except ImportError as exc:
    validate_software_archive = None
    validate_software_manifest = None
    PRODUCTION_VALIDATOR_ERROR = exc
else:
    PRODUCTION_VALIDATOR_ERROR = None

OWNER = "Chosen11111111"
REPOSITORY = "wogua"
BRANCH = "main"
API_BASE = "https://api.github.com"
RAW_MANIFEST_URL = "https://raw.githubusercontent.com/Chosen11111111/wogua/main/software-manifest.json"
MANIFEST_PATH = ROOT / "software-manifest.json"
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


def _enable_system_tls() -> None:
    if truststore is not None:
        truststore.inject_into_ssl()


def _safe_json_response(response: requests.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise ReleaseError(f"GitHub 返回的不是 JSON: HTTP {getattr(response, 'status_code', 'unknown')}") from exc
    if not isinstance(payload, dict):
        raise ReleaseError("GitHub 返回的数据格式错误")
    return payload


def _credential_token() -> str:
    request = "protocol=https\nhost=github.com\n\n"
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            cwd=ROOT,
            input=request,
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError("无法读取 GitHub 凭据，请先登录 Git Credential Manager") from exc
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    token = values.get("password", "").strip()
    if not token:
        raise ReleaseError("GitHub 凭据中没有可用 token")
    return token


def _github_headers(token: str, content_type: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _raise_http(response: requests.Response, action: str) -> None:
    status_code = getattr(response, "status_code", 0)
    if 200 <= status_code < 300:
        return
    detail = ""
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail = str(payload.get("message", ""))
    except (AttributeError, ValueError, TypeError):
        detail = str(getattr(response, "text", ""))[:200]
    suffix = f" {detail}" if detail else ""
    raise ReleaseError(f"{action}失败: HTTP {status_code or 'unknown'}{suffix}")


def _github_get_release(tag: str, token: str, request_get: Callable | None = None) -> dict | None:
    get = request_get or requests.get
    try:
        response = get(
            f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/releases/tags/{quote(tag, safe='')}",
            headers=_github_headers(token),
            timeout=30,
        )
    except requests.RequestException as exc:
        raise ReleaseError(f"查询 GitHub Release 失败: {exc}") from exc
    if getattr(response, "status_code", 0) == 404:
        return None
    _raise_http(response, "查询 GitHub Release")
    return _safe_json_response(response)


def _github_tag_exists(tag: str, token: str, request_get: Callable | None = None) -> bool:
    get = request_get or requests.get
    try:
        response = get(
            f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/git/ref/tags/{quote(tag, safe='')}",
            headers=_github_headers(token),
            timeout=30,
        )
    except requests.RequestException as exc:
        raise ReleaseError(f"查询 GitHub tag 失败: {exc}") from exc
    if getattr(response, "status_code", 0) == 404:
        return False
    _raise_http(response, "查询 GitHub tag")
    return True


def _verify_github_asset(asset: Mapping[str, object], info: PackageInfo) -> None:
    if asset.get("state") != "uploaded":
        raise ReleaseError("GitHub ZIP 资产状态不是 uploaded")
    try:
        size = int(asset.get("size", -1))
    except (TypeError, ValueError) as exc:
        raise ReleaseError("GitHub ZIP 资产大小无效") from exc
    if size != info.size:
        raise ReleaseError(f"GitHub ZIP 资产大小不匹配: {size} != {info.size}")
    digest = str(asset.get("digest", "")).strip().casefold()
    expected = f"sha256:{info.sha256}".casefold()
    if digest != expected:
        raise ReleaseError("GitHub ZIP 资产 SHA-256 不匹配")


def publish_github_release(
    info: PackageInfo,
    log: Callable[[str], None] = print,
    *,
    token: str | None = None,
    request_get: Callable | None = None,
    request_post: Callable | None = None,
) -> dict[str, object]:
    _enable_system_tls()
    token = token or _credential_token()
    get = request_get or requests.get
    post = request_post or requests.post
    release = _github_get_release(info.tag, token, get)
    if release is not None or _github_tag_exists(info.tag, token, get):
        raise ReleaseError(f"GitHub Release 或 tag 已存在: {info.tag}，为避免覆盖已停止")
    try:
        response = post(
            f"{API_BASE}/repos/{OWNER}/{REPOSITORY}/releases",
            headers=_github_headers(token),
            json={
                "tag_name": info.tag,
                "target_commitish": BRANCH,
                "name": f"Chosen {info.version}",
                "body": f"Chosen software package release {info.version}.",
                "draft": False,
                "prerelease": False,
            },
            timeout=30,
        )
    except requests.RequestException as exc:
        raise ReleaseError(f"创建 GitHub Release 失败: {exc}") from exc
    _raise_http(response, "创建 GitHub Release")
    release = _safe_json_response(response)
    log(f"GitHub Release 已创建: {release.get('html_url', info.tag)}")

    raw_upload_url = release.get("upload_url")
    upload_url = str(raw_upload_url or "").split("{", 1)[0].rstrip("?&")
    if not upload_url:
        raise ReleaseError("GitHub 没有返回资产上传地址")
    upload_url = _validate_https_url(upload_url)
    separator = "&" if "?" in upload_url else "?"
    try:
        with info.archive_path.open("rb") as stream:
            response = post(
                f"{upload_url}{separator}name={quote(info.asset_name, safe='')}",
                headers={**_github_headers(token, "application/zip"), "Content-Length": str(info.size)},
                data=stream,
                timeout=(30, 900),
            )
    except OSError as exc:
        raise ReleaseError(f"打开软件 ZIP 失败: {exc}") from exc
    except requests.RequestException as exc:
        raise ReleaseError(f"上传 GitHub ZIP 失败: {exc}") from exc
    _raise_http(response, "上传 GitHub ZIP 资产")
    asset = _safe_json_response(response)
    _verify_github_asset(asset, info)
    log(f"GitHub ZIP 上传完成: {asset.get('name', info.asset_name)} ({info.size} bytes)")
    return {"created": True, "uploaded": True, "release": release, "asset": asset}


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


def check_download_range(
    url: str,
    expected_size: int,
    http_get: Callable | None = None,
    log: Callable[[str], None] = print,
    label: str = "CDN",
) -> bool:
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size <= 0:
        raise ReleaseError("期望的软件包大小无效")
    value = _validate_https_url(url)
    get = http_get or requests.get
    sample_size = min(1024 * 1024, expected_size)
    response = None
    started = time.monotonic()
    try:
        response = get(
            value,
            headers={**DOWNLOAD_HEADERS, "Range": f"bytes=0-{sample_size - 1}"},
            stream=True,
            allow_redirects=False,
            verify=True,
            timeout=(20, 60),
        )
        if getattr(response, "status_code", 0) != 206:
            raise ReleaseError(f"{label}测试状态码不是 206，而是 {getattr(response, 'status_code', 'unknown')}")
        headers = getattr(response, "headers", {}) or {}
        content_range = headers.get("Content-Range") or headers.get("content-range")
        match = CONTENT_RANGE_RE.fullmatch(str(content_range).strip())
        expected_range = (0, sample_size - 1, expected_size)
        if match is None or tuple(map(int, match.groups())) != expected_range:
            raise ReleaseError(f"{label}返回范围不匹配: {content_range}")
        content_length = headers.get("Content-Length") or headers.get("content-length")
        if content_length not in (None, "") and int(content_length) != sample_size:
            raise ReleaseError(f"{label}返回长度不匹配: {content_length}")
        raw = getattr(response, "raw", None)
        if raw is not None and callable(getattr(raw, "read", None)):
            data = raw.read(sample_size)
        else:
            content = getattr(response, "content", None)
            if content:
                data = bytes(content)[:sample_size]
            else:
                chunks = []
                for chunk in response.iter_content(sample_size):
                    if chunk:
                        chunks.append(chunk)
                data = b"".join(chunks)[:sample_size]
        if len(data) != sample_size:
            raise ReleaseError(f"{label}返回内容不完整: {len(data)} bytes")
        if data[:4] != b"PK\x03\x04":
            raise ReleaseError(f"{label}返回内容不是 ZIP")
        elapsed = max(time.monotonic() - started, 0.001)
        log(f"{label}测试通过: 206, {len(data)} bytes, {len(data) / 1024 / 1024 / elapsed:.2f} MB/s")
        return True
    except ReleaseError:
        raise
    except (OSError, requests.RequestException, TypeError, ValueError) as exc:
        raise ReleaseError(f"{label}测试失败: {exc}") from exc
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


def _run_git(args: list[str], action: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=True,
            timeout=120,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = (getattr(exc, "stderr", "") or str(exc)).strip()
        raise ReleaseError(f"{action}失败: {detail[-400:]}") from exc
    return result.stdout.strip()


def write_and_push_manifest(
    info: PackageInfo,
    log: Callable[[str], None] = print,
    manifest_path: Path | str = MANIFEST_PATH,
) -> str:
    target = Path(manifest_path).expanduser().resolve()
    if target != MANIFEST_PATH.resolve():
        raise ReleaseError("自动推送时 manifest 必须是 wogua/software-manifest.json")
    try:
        git_path = target.relative_to(ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise ReleaseError("manifest 必须位于 wogua Git 仓库内") from exc
    staged_before = _run_git(["diff", "--cached", "--name-only"], "检查 Git 暂存区")
    if staged_before:
        raise ReleaseError("Git 暂存区已有其他变更，未自动处理")
    manifest = build_manifest(info)
    if validate_software_manifest is None:
        raise ReleaseError("无法加载 Chosen 软件 manifest 校验器") from PRODUCTION_VALIDATOR_ERROR
    try:
        validate_software_manifest(manifest)
    except Exception as exc:
        raise ReleaseError(f"生成的 manifest 未通过生产校验: {exc}") from exc
    write_manifest(target, manifest)
    _run_git(["add", "--", git_path], "暂存 manifest")
    staged = _run_git(["diff", "--cached", "--name-only"], "检查 manifest 暂存区")
    if staged not in ("", git_path):
        _run_git(["reset"], "清理错误暂存")
        raise ReleaseError("暂存区不是只有软件 manifest")
    if not staged:
        log("manifest 内容未变化，无需新建提交")
        return _run_git(["rev-parse", "HEAD"], "读取当前提交")
    _run_git(["diff", "--cached", "--check"], "检查 manifest 格式")
    _run_git(["commit", "-m", f"release: publish Chosen {info.version} manifest"], "提交 manifest")
    _run_git(["push", "origin", BRANCH], "推送 manifest")
    commit = _run_git(["rev-parse", "HEAD"], "读取提交哈希")
    log(f"manifest 已推送到 {BRANCH}: {commit}")
    return commit


def verify_remote(info: PackageInfo, log: Callable[[str], None] = print) -> None:
    _enable_system_tls()
    try:
        response = requests.get(
            RAW_MANIFEST_URL + f"?version={quote(info.version, safe='')}",
            headers={"Accept": "application/json", "Cache-Control": "no-cache"},
            timeout=30,
        )
    except requests.RequestException as exc:
        raise ReleaseError(f"读取远程 manifest 失败: {exc}") from exc
    _raise_http(response, "读取远程 manifest")
    remote = _safe_json_response(response)
    expected = build_manifest(info)
    for key in ("version", "download_url", "download_url_backup", "size", "sha256", "release_tag"):
        if remote.get(key) != expected[key]:
            raise ReleaseError(f"远程 manifest 字段不匹配: {key}")
    log("远程 manifest 验证通过")


def publish(
    zip_path: Path | str,
    manifest_path: Path | str | None = MANIFEST_PATH,
    *,
    client=None,
    http_get: Callable | None = None,
    token: str | None = None,
    github_get: Callable | None = None,
    github_post: Callable | None = None,
    log: Callable[[str], None] = print,
    push_manifest: bool = True,
    verify_remote_manifest: bool = True,
) -> dict[str, object]:
    _enable_system_tls()
    log("开始检查软件 ZIP")
    info = inspect_package(zip_path)
    log(f"ZIP 校验通过: {info.asset_name}，版本 {info.version}，{info.size} bytes")
    upload_result = upload_to_r2(info, client=client)
    if upload_result.get("uploaded"):
        log(f"R2 上传完成: {upload_result['key']}")
    else:
        log(f"R2 已存在且大小一致: {upload_result['key']}")
    check_download_range(r2_public_url(info), info.size, http_get=http_get, log=log, label="CDN")
    github_result = publish_github_release(
        info,
        log,
        token=token,
        request_get=github_get,
        request_post=github_post,
    )
    check_download_range(
        f"{PROXY_PREFIX}{info.github_url}",
        info.size,
        http_get=http_get,
        log=log,
        label="GitHub v4 代理",
    )
    manifest = build_manifest(info)
    commit = None
    if manifest_path is not None:
        if push_manifest:
            commit = write_and_push_manifest(info, log, manifest_path)
        else:
            write_manifest(manifest_path, manifest)
            log(f"manifest 已写入: {manifest_path}")
    elif push_manifest:
        raise ReleaseError("启用 manifest 推送时必须提供 manifest 路径")
    if verify_remote_manifest and push_manifest:
        verify_remote(info, log)
    log("软件发布流程完成")
    return {
        "info": info,
        "r2": upload_result,
        "github": github_result,
        "manifest": manifest,
        "commit": commit,
    }


class SoftwareReleaseApp(Tk):
    def __init__(self):
        super().__init__()
        self.title("Chosen 软件发布")
        self.geometry("760x520")
        self.minsize(680, 450)
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.running = False
        self.path_var = StringVar()
        self.status_var = StringVar(value="请选择 Chosen 软件 ZIP")
        self._build_widgets()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._drain_events)

    def _build_widgets(self) -> None:
        file_frame = Frame(self)
        file_frame.pack(fill=X, padx=12, pady=(12, 6))
        Label(file_frame, text="软件 ZIP").pack(side=LEFT, padx=(0, 8))
        Entry(file_frame, textvariable=self.path_var).pack(side=LEFT, fill=X, expand=True)
        self.choose_button = Button(file_frame, text="选择 ZIP", command=self._choose_zip)
        self.choose_button.pack(side=LEFT, padx=(8, 0))
        self.start_button = Button(self, text="开始发布", command=self._start_publish)
        self.start_button.pack(anchor="w", padx=12, pady=(0, 6))
        Label(self, textvariable=self.status_var, anchor="w").pack(fill=X, padx=12, pady=(0, 6))
        self.log_text = ScrolledText(self, height=22, state="disabled")
        self.log_text.pack(fill=BOTH, expand=True, padx=12, pady=(0, 12))

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert(END, message + "\n")
        self.log_text.see(END)
        self.log_text.configure(state="disabled")

    def _queue_log(self, message: str) -> None:
        self.events.put(("log", message))

    def _choose_zip(self) -> None:
        if self.running:
            return
        path = filedialog.askopenfilename(
            title="选择 Chosen 软件 ZIP",
            filetypes=[("Chosen 软件包", "*.zip"), ("ZIP 文件", "*.zip")],
        )
        if not path:
            return
        try:
            info = inspect_package(path)
        except Exception as exc:
            self.status_var.set("ZIP 校验失败")
            messagebox.showerror("ZIP 校验失败", str(exc))
            return
        self.path_var.set(str(info.archive_path))
        self.status_var.set(f"已校验：版本 {info.version}，{info.size} bytes")
        self._append_log(f"已选择并校验: {info.archive_path}")
        self._append_log(f"SHA-256: {info.sha256}")

    def _start_publish(self) -> None:
        if self.running:
            return
        raw_path = self.path_var.get().strip()
        if not raw_path:
            messagebox.showwarning("请选择 ZIP", "请先选择软件 ZIP")
            return
        path = Path(raw_path)
        if not path.is_file():
            messagebox.showerror("文件不存在", str(path))
            return
        self.running = True
        self.start_button.configure(state="disabled")
        self.choose_button.configure(state="disabled")
        self.status_var.set("正在发布，请等待日志完成")
        threading.Thread(target=self._publish_worker, args=(path,), daemon=True).start()

    def _publish_worker(self, path: Path) -> None:
        try:
            result = publish(path, MANIFEST_PATH, log=self._queue_log)
            info = result["info"]
            self.events.put(("success", f"发布完成：{info.version} / {info.asset_name}"))
        except Exception as exc:
            self.events.put(("error", str(exc)))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, message = self.events.get_nowait()
                if kind == "log":
                    self._append_log(message)
                elif kind == "success":
                    self._append_log(message)
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.choose_button.configure(state="normal")
                    self.status_var.set("发布完成")
                    messagebox.showinfo("发布完成", message)
                elif kind == "error":
                    self._append_log("发布失败: " + message)
                    self.running = False
                    self.start_button.configure(state="normal")
                    self.choose_button.configure(state="normal")
                    self.status_var.set("发布失败，manifest 未必已推送")
                    messagebox.showerror("发布失败", message)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _on_close(self) -> None:
        if self.running:
            messagebox.showwarning("发布进行中", "发布尚未完成，请等待流程结束")
            return
        self.destroy()


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        SoftwareReleaseApp().mainloop()
        return 0
    parser = argparse.ArgumentParser(description="发布 Chosen 软件 ZIP 到 CDN、R2 和 GitHub Release")
    parser.add_argument("zip_path", type=Path, help="Chosen 软件 ZIP 路径")
    parser.add_argument("--write-manifest", dest="manifest_path", type=Path, default=MANIFEST_PATH, help="manifest 路径，默认写入当前目录")
    parser.add_argument("--no-push", action="store_true", help="只写 manifest，不提交或推送 Git")
    parser.add_argument("--no-remote-check", action="store_true", help="跳过远程 manifest 核验")
    args = parser.parse_args(arguments)
    try:
        result = publish(
            args.zip_path,
            args.manifest_path,
            log=print,
            push_manifest=not args.no_push,
            verify_remote_manifest=not args.no_remote_check,
        )
    except ReleaseError as exc:
        print(f"发布失败: {exc}", file=sys.stderr)
        return 1
    info = result["info"]
    print(f"软件包校验通过: {info.asset_name} {info.version} {info.size} bytes")
    print(f"CDN 地址: {r2_public_url(info)}")
    print(f"GitHub Release: {info.github_url}")
    if result.get("commit"):
        print(f"manifest 提交: {result['commit']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
