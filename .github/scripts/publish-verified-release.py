"""Publish reviewed binary artifacts, never overwrite an existing release."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPO = "heros20/label2a4-releases"
API = "https://api.github.com/repos/" + REPO
assert os.environ["GITHUB_REPOSITORY"] == REPO
assert os.environ["GITHUB_REF_NAME"] == "main"
config = json.loads(Path(".github/release-request.json").read_text())
version = config["version"]
assert re.fullmatch(r"0\.3\.\d+", version)
tag = "desktop-v" + version
setup = "Label2A4.Bureau.Setup." + version + ".exe"
expected_names = {setup, setup + ".blockmap", "latest.yml"}
assert set(config["files"]) == expected_names
source = urllib.parse.urlparse(config["archive_url"])
assert source.scheme == "https" and source.hostname.endswith(".oaiusercontent.com")
subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location", "--max-time", "180", config["archive_url"], "-o", "release-artifacts.zip"], check=True)
archive_bytes = Path("release-artifacts.zip").read_bytes()
assert hashlib.sha256(archive_bytes).hexdigest() == config["archive_sha256"]
with zipfile.ZipFile("release-artifacts.zip") as archive:
    entries = [entry for entry in archive.infolist() if not entry.is_dir()]
    assert {entry.filename for entry in entries} == expected_names
    assert len(entries) == len(expected_names)
    payloads = {}
    for entry in entries:
        expected = config["files"][entry.filename]
        assert entry.file_size == expected["size"] and entry.file_size < 300_000_000
        payload = archive.read(entry)
        assert hashlib.sha256(payload).hexdigest() == expected["sha256"]
        assert base64.b64encode(hashlib.sha512(payload).digest()).decode() == expected["sha512"]
        payloads[entry.filename] = payload
assert payloads[setup][:2] == b"MZ"
metadata = payloads["latest.yml"].decode("utf-8-sig")
assert re.search(r"^version: " + re.escape(version) + r"\s*$", metadata, re.M)
assert re.search(r"^path: " + re.escape(setup) + r"\s*$", metadata, re.M)
assert re.search(r"^sha512: " + re.escape(config["files"][setup]["sha512"]) + r"\s*$", metadata, re.M)

def request(method, endpoint, data=None, binary=None, content_type=None):
    url = API + endpoint
    if binary is not None:
        assert re.fullmatch(r"/releases/\d+/assets\?name=[A-Za-z0-9.%_-]+", endpoint)
        url = "https://uploads.github.com/repos/" + REPO + endpoint
    headers = {"Authorization": "Bearer " + os.environ["GH_TOKEN"], "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Label2A4-release-verifier"}
    body = binary if binary is not None else json.dumps(data).encode() if data is not None else None
    if body is not None:
        headers["Content-Type"] = content_type or "application/json"
    req = urllib.request.Request(url, method=method, headers=headers, data=body)
    with urllib.request.urlopen(req, timeout=240) as response:
        return json.load(response)

try:
    request("GET", "/releases/tags/" + tag)
except urllib.error.HTTPError as error:
    if error.code != 404:
        raise
else:
    raise RuntimeError("Release already exists; refusing to overwrite it")

release = request("POST", "/releases", {"tag_name": tag, "target_commitish": "main", "name": "Label2A4 Bureau " + version, "body": config["notes"], "draft": True, "prerelease": False})
release_id = release["id"]
for name in [setup, setup + ".blockmap", "latest.yml"]:
    result = request("POST", "/releases/" + str(release_id) + "/assets?name=" + urllib.parse.quote(name, safe=""), binary=payloads[name], content_type="application/octet-stream")
    assert result["name"] == name and result["size"] == config["files"][name]["size"]
    if result.get("digest"):
        assert result["digest"] == "sha256:" + config["files"][name]["sha256"]
assets = request("GET", "/releases/" + str(release_id) + "/assets")
assert {asset["name"] for asset in assets} == expected_names
published = request("PATCH", "/releases/" + str(release_id), {"draft": False, "prerelease": False, "make_latest": "true"})
assert published["draft"] is False and published["tag_name"] == tag
verified = []
for name in sorted(expected_names):
    url = "https://github.com/" + REPO + "/releases/download/" + tag + "/" + name
    destination = Path("verify-" + name)
    for attempt in range(6):
        result = subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location", "--max-time", "180", url, "-o", str(destination)])
        if result.returncode == 0:
            break
        time.sleep(5)
    else:
        raise RuntimeError("Published asset is not publicly downloadable: " + name)
    content = destination.read_bytes()
    assert len(content) == config["files"][name]["size"]
    assert hashlib.sha256(content).hexdigest() == config["files"][name]["sha256"]
    verified.append({"name": name, "url": url, **config["files"][name]})
latest = request("GET", "/releases/latest")
assert latest["id"] == release_id
Path("release-receipt.json").write_text(json.dumps({"release_id": release_id, "version": version, "release_url": published["html_url"], "public_downloads_verified": verified}, indent=2))
print("Published and verified:", published["html_url"])
