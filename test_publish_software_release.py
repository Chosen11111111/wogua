import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from publish_software_release import (
    PackageInfo,
    ReleaseError,
    build_manifest,
    check_download_range,
    inspect_package,
    publish,
    publish_github_release,
    r2_object_key,
    r2_public_url,
    upload_to_r2,
    write_manifest,
)


class FakeClientError(Exception):
    def __init__(self, code):
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class FakeR2Client:
    def __init__(self, existing_size=None):
        self.existing_size = existing_size
        self.put_calls = 0
        self.head_calls = 0
        self.upload_args = None

    def head_object(self, Bucket, Key):
        self.head_calls += 1
        if self.existing_size is None:
            raise FakeClientError("404")
        return {"ContentLength": self.existing_size}

    def upload_file(self, filename, bucket, key, ExtraArgs):
        self.put_calls += 1
        self.upload_args = (filename, bucket, key, ExtraArgs)
        self.existing_size = Path(filename).stat().st_size


class FakeHttpResponse:
    def __init__(self, status_code, headers, body=None):
        self.status_code = status_code
        self.headers = headers
        self.raw = io.BytesIO(body or b"")
        self.closed = False

    def close(self):
        self.closed = True


class FakeJsonResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self.payload


class PublishSoftwareReleaseTests(unittest.TestCase):
    def make_valid_software_zip(self, version="2.10"):
        handle = tempfile.NamedTemporaryFile(prefix="Chosen", suffix=".zip", delete=False)
        handle.close()
        path = Path(handle.name)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("Chosen.exe", b"exe")
            archive.writestr("ChosenUpdater.exe", b"updater")
            archive.writestr("_internal/runtime.dat", b"runtime")
            archive.writestr("_internal/version.json", json.dumps({"version": version}))
        return path

    def test_inspect_package_reads_version_size_and_sha256(self):
        path = self.make_valid_software_zip()
        try:
            info = inspect_package(path)
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(info.version, "2.10")
            self.assertEqual(info.archive_path, path.resolve())
            self.assertEqual(info.size, path.stat().st_size)
            self.assertEqual(info.sha256, digest)
        finally:
            path.unlink()

    def test_inspect_package_rejects_invalid_software_zip(self):
        handle = tempfile.NamedTemporaryFile(suffix=".zip", delete=False)
        handle.write(b"not a zip")
        handle.close()
        path = Path(handle.name)
        try:
            with self.assertRaises(ReleaseError):
                inspect_package(path)
        finally:
            path.unlink()

    def test_r2_paths_use_wogua_prefix_and_versioned_asset_name(self):
        info = PackageInfo("2.10", Path("Chosen2.10.zip"), 219176976, "a" * 64)
        self.assertEqual(r2_object_key(info), "wogua/Chosen2.10.zip")
        self.assertEqual(r2_public_url(info), "https://cdn.chosen.cc.cd/wogua/Chosen2.10.zip")

    def test_build_manifest_puts_r2_first_and_github_proxies_after_it(self):
        info = PackageInfo("2.10", Path("Chosen2.10.zip"), 219176976, "a" * 64)
        manifest = build_manifest(info)
        self.assertEqual(manifest["download_url"], "https://cdn.chosen.cc.cd/wogua/Chosen2.10.zip")
        self.assertEqual(manifest["size"], 219176976)
        self.assertEqual(manifest["release_tag"], "v2.10")
        self.assertEqual(len(manifest["download_url_backup"]), 3)
        self.assertTrue(all(url.startswith("https://") for url in manifest["download_url_backup"]))
        self.assertTrue(all("releases/download/v2.10/Chosen2.10.zip" in url for url in manifest["download_url_backup"]))

    def test_upload_to_r2_rejects_same_key_with_different_size(self):
        info = PackageInfo("2.10", Path("Chosen2.10.zip"), 219176976, "a" * 64)
        client = FakeR2Client(existing_size=1)
        with patch.dict(os.environ, {"R2_BUCKET": "software"}, clear=False):
            with self.assertRaises(ReleaseError):
                upload_to_r2(info, client=client)
        self.assertEqual(client.put_calls, 0)

    def test_upload_to_r2_keeps_same_size_object_without_overwrite(self):
        info = PackageInfo("2.10", Path("Chosen2.10.zip"), 219176976, "a" * 64)
        client = FakeR2Client(existing_size=info.size)
        with patch.dict(os.environ, {"R2_BUCKET": "software"}, clear=False):
            result = upload_to_r2(info, client=client)
        self.assertFalse(result["uploaded"])
        self.assertEqual(client.put_calls, 0)

    def test_upload_to_r2_uploads_missing_object_and_checks_size(self):
        path = self.make_valid_software_zip()
        try:
            info = inspect_package(path)
            client = FakeR2Client()
            with patch.dict(os.environ, {"R2_BUCKET": "software"}, clear=False):
                result = upload_to_r2(info, client=client)
            self.assertTrue(result["uploaded"])
            self.assertEqual(client.put_calls, 1)
            self.assertEqual(client.upload_args[1:], ("software", "wogua/" + path.name, {"ContentType": "application/zip"}))
            self.assertEqual(client.head_calls, 2)
        finally:
            path.unlink()

    def test_public_range_check_requires_206_and_exact_content_range(self):
        response = FakeHttpResponse(
            206,
            {"Content-Range": "bytes 0-1048575/219176976", "Content-Length": "1048576"},
            b"PK\x03\x04" + b"x" * (1024 * 1024 - 4),
        )
        result = check_download_range(
            "https://cdn.chosen.cc.cd/wogua/Chosen2.10.zip",
            219176976,
            http_get=lambda *args, **kwargs: response,
        )
        self.assertTrue(result)
        self.assertTrue(response.closed)

    def test_public_range_check_rejects_wrong_response(self):
        response = FakeHttpResponse(200, {"Content-Length": "219176976"})
        with self.assertRaises(ReleaseError):
            check_download_range(
                "https://cdn.chosen.cc.cd/wogua/Chosen2.10.zip",
                219176976,
                http_get=lambda *args, **kwargs: response,
            )
        self.assertTrue(response.closed)

    def test_publish_github_release_creates_release_and_uploads_zip(self):
        path = self.make_valid_software_zip()
        try:
            info = inspect_package(path)
            get_responses = [FakeJsonResponse(404, {}), FakeJsonResponse(404, {})]
            get_calls = []
            post_calls = []
            release_payload = {
                "tag_name": info.tag,
                "html_url": "https://github.com/Chosen11111111/wogua/releases/tag/" + info.tag,
                "upload_url": "https://uploads.github.com/repos/Chosen11111111/wogua/releases/1/assets{?name,label}",
            }
            asset_payload = {
                "name": info.asset_name,
                "state": "uploaded",
                "size": info.size,
                "digest": "sha256:" + info.sha256,
            }

            def fake_get(url, **kwargs):
                get_calls.append((url, kwargs))
                return get_responses.pop(0)

            def fake_post(url, **kwargs):
                post_calls.append((url, kwargs))
                if url.endswith("/releases"):
                    self.assertEqual(kwargs["json"]["tag_name"], info.tag)
                    return FakeJsonResponse(201, release_payload)
                self.assertIn("?name=" + info.asset_name, url)
                self.assertEqual(kwargs["headers"]["Content-Type"], "application/zip")
                self.assertEqual(kwargs["headers"]["Content-Length"], str(info.size))
                self.assertEqual(kwargs["data"].read(), path.read_bytes())
                return FakeJsonResponse(201, asset_payload)

            result = publish_github_release(
                info,
                token="test-token",
                request_get=fake_get,
                request_post=fake_post,
            )
            self.assertTrue(result["created"])
            self.assertTrue(result["uploaded"])
            self.assertEqual(len(get_calls), 2)
            self.assertEqual(len(post_calls), 2)
        finally:
            path.unlink()

    def test_publish_github_release_stops_when_release_exists(self):
        path = self.make_valid_software_zip()
        try:
            info = inspect_package(path)

            def fake_get(url, **kwargs):
                return FakeJsonResponse(200, {"tag_name": info.tag})

            with self.assertRaises(ReleaseError):
                publish_github_release(
                    info,
                    token="test-token",
                    request_get=fake_get,
                    request_post=lambda *args, **kwargs: self.fail("existing release must not upload"),
                )
        finally:
            path.unlink()

    def test_publish_runs_r2_github_proxy_and_manifest_in_order(self):
        path = self.make_valid_software_zip()
        try:
            info = inspect_package(path)
            r2_client = FakeR2Client()
            range_calls = []
            range_body = b"PK\x03\x04" + b"x" * (info.size - 4)

            def fake_http(url, **kwargs):
                range_calls.append((url, kwargs))
                return FakeHttpResponse(
                    206,
                    {
                        "Content-Range": f"bytes 0-{info.size - 1}/{info.size}",
                        "Content-Length": str(info.size),
                    },
                    range_body,
                )

            github_get_responses = [FakeJsonResponse(404, {}), FakeJsonResponse(404, {})]
            github_posts = []
            release_payload = {
                "tag_name": info.tag,
                "html_url": "https://github.com/Chosen11111111/wogua/releases/tag/" + info.tag,
                "upload_url": "https://uploads.github.com/repos/Chosen11111111/wogua/releases/1/assets{?name,label}",
            }
            asset_payload = {
                "name": info.asset_name,
                "state": "uploaded",
                "size": info.size,
                "digest": "sha256:" + info.sha256,
            }

            def fake_github_get(url, **kwargs):
                return github_get_responses.pop(0)

            def fake_github_post(url, **kwargs):
                github_posts.append(url)
                if url.endswith("/releases"):
                    return FakeJsonResponse(201, release_payload)
                return FakeJsonResponse(201, asset_payload)

            with tempfile.TemporaryDirectory() as directory:
                result = publish(
                    path,
                    Path(directory) / "software-manifest.json",
                    client=r2_client,
                    http_get=fake_http,
                    token="test-token",
                    github_get=fake_github_get,
                    github_post=fake_github_post,
                    log=lambda message: None,
                    push_manifest=False,
                    verify_remote_manifest=False,
                )
                self.assertTrue(result["github"]["uploaded"])
                self.assertTrue((Path(directory) / "software-manifest.json").is_file())
            self.assertEqual(r2_client.put_calls, 1)
            self.assertEqual(len(range_calls), 2)
            self.assertTrue(all(call[1]["headers"]["Range"] == f"bytes=0-{info.size - 1}" for call in range_calls))
            self.assertEqual(len(github_posts), 2)
        finally:
            path.unlink()

    def test_write_manifest_uses_utf8_json_and_replaces_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "software-manifest.json"
            payload = {"version": "2.10", "message": "软件包"}
            write_manifest(path, payload)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)
            self.assertFalse(list(path.parent.glob(".*.tmp")))


if __name__ == "__main__":
    unittest.main()
