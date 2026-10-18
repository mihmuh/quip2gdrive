#!/usr/bin/env python3
"""Export Quip documents and spreadsheets into a folder tree ready to upload to Google Drive.

The Quip token is read from the macOS Keychain (see secrets_setup.py). Console
output contains only Quip IDs and counts, never titles or contents.
Re-running resumes: unchanged threads are skipped, edited ones re-exported.

    python quip_export.py                      # everything you can reach
    python quip_export.py --limit 5            # trial run on a few documents
    python quip_export.py --folder FOLDER_ID   # one folder only
"""
import argparse
import json
import os
import shutil
import sys
import time
from datetime import date

import requests

from secrets_setup import require_token

DEFAULT_API_BASE = os.environ.get("QUIP_API_BASE", "https://platform.quip.com")
DEFAULT_STAGING = os.path.join(os.path.dirname(os.path.abspath(__file__)), "staging")
EXPORT_FORMATS = {"document": "docx", "spreadsheet": "xlsx"}
TREE_DIR = "upload"
UNFILED = "_Unfiled"
MAX_NAME_BYTES = 200
RECENT_PAGE_SIZE = 50
BULK_BATCH_SIZE = 20


class QuipError(Exception):
    def __init__(self, status, path, detail=""):
        super().__init__(f"HTTP {status} on {path} {detail}".strip())
        self.status = status


class QuipClient:
    """Minimal Quip REST client. Never logs headers, so the token stays out of output."""

    def __init__(self, token, base=DEFAULT_API_BASE, max_attempts=8):
        self.base = base.rstrip("/")
        self.max_attempts = max_attempts
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"

    def get(self, path, params=None, raw=False):
        for attempt in range(self.max_attempts):
            try:
                r = self.session.get(self.base + path, params=params, timeout=180)
            except requests.RequestException as e:
                # Report only the exception type: some messages embed request details.
                wait = min(2 ** attempt, 60)
                print(f"  network error ({type(e).__name__}), retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            self._respect_rate_limit(r)
            if r.status_code == 429 or r.status_code >= 500:
                wait = self._retry_after(r, attempt)
                print(f"  HTTP {r.status_code} on {path}, retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if r.status_code != 200:
                raise QuipError(r.status_code, path, self._error_detail(r))
            return r.content if raw else r.json()
        raise QuipError("retries-exhausted", path)

    @staticmethod
    def _respect_rate_limit(r):
        remaining = r.headers.get("X-Ratelimit-Remaining")
        reset = r.headers.get("X-Ratelimit-Reset")
        if remaining is not None and reset is not None and int(remaining) <= 1:
            wait = max(0.0, float(reset) - time.time()) + 1
            print(f"  rate limit reached, pausing {wait:.0f}s", file=sys.stderr)
            time.sleep(wait)

    @staticmethod
    def _retry_after(r, attempt):
        reset = r.headers.get("X-Ratelimit-Reset")
        if reset:
            return max(1, int(float(reset) - time.time()) + 1)
        return min(2 ** attempt, 60)

    @staticmethod
    def _error_detail(r):
        try:
            return str(r.json().get("error_description", ""))[:200]
        except ValueError:
            return ""


def walk_folders(client, root_ids):
    """Breadth-first walk. Returns (folders, {thread id: folder id}).

    Quip folders behave like tags, so a thread can sit in several folders;
    it is placed in the first one found.
    """
    folders, placements, queue = {}, {}, [(fid, None) for fid in root_ids]
    while queue:
        fid, parent = queue.pop(0)
        if fid in folders:
            continue
        try:
            data = client.get(f"/1/folders/{fid}")
        except QuipError as e:
            print(f"  skipping folder {fid}: {e}", file=sys.stderr)
            continue
        folders[fid] = {"title": data.get("folder", {}).get("title") or fid, "parent": parent}
        for child in data.get("children", []):
            if child.get("restricted"):
                continue
            if "folder_id" in child:
                queue.append((child["folder_id"], fid))
            elif "thread_id" in child:
                placements.setdefault(child["thread_id"], fid)
    return folders, placements


def recent_thread_ids(client, max_pages):
    """Page backwards through /threads/recent to catch threads in no reachable folder."""
    ids, before = [], None
    for _ in range(max_pages):
        params = {"count": RECENT_PAGE_SIZE}
        if before is not None:
            params["max_updated_usec"] = before
        page = client.get("/1/threads/recent", params=params)
        if not page:
            break
        ids.extend(page.keys())
        oldest = min(item["thread"]["updated_usec"] for item in page.values())
        if before is not None and oldest >= before:
            break
        before = oldest - 1
        if len(page) < RECENT_PAGE_SIZE:
            break
    return ids


def fetch_metadata(client, thread_ids):
    meta = {}
    for i in range(0, len(thread_ids), BULK_BATCH_SIZE):
        batch = thread_ids[i:i + BULK_BATCH_SIZE]
        try:
            result = client.get("/1/threads/", params={"ids": ",".join(batch)})
        except QuipError:
            result = {}
            for tid in batch:
                try:
                    result[tid] = client.get(f"/1/threads/{tid}")
                except QuipError as e:
                    print(f"  no metadata for {tid}: {e}", file=sys.stderr)
        for tid, item in result.items():
            t = item.get("thread", {})
            meta[tid] = {
                "title": t.get("title") or "Untitled",
                "type": t.get("type", "unknown"),
                "updated_usec": t.get("updated_usec", 0),
            }
    return meta


def export_thread(client, tid, kind, staging):
    """Download one thread. Returns (relative file path, format)."""
    fmt = EXPORT_FORMATS[kind]
    try:
        content = client.get(f"/1/threads/{tid}/export/{fmt}", raw=True)
    except QuipError as e:
        if kind != "document":
            raise
        # Fall back to the document's HTML, which Google Docs can open.
        print(f"  docx export failed for {tid} ({e}); falling back to HTML", file=sys.stderr)
        content = client.get(f"/1/threads/{tid}").get("html", "").encode("utf-8")
        fmt = "html"
    rel = os.path.join("files", f"{tid}.{fmt}")
    with open(os.path.join(staging, rel), "wb") as f:
        f.write(content)
    return rel, fmt


def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def run(client, staging, folder_ids=None, include_recent=True, recent_pages=200, limit=None, top_name=None):
    files_dir = os.path.join(staging, "files")
    os.makedirs(files_dir, exist_ok=True)
    os.chmod(staging, 0o700)
    manifest_path = os.path.join(staging, "manifest.json")
    manifest = load_json(manifest_path, {"folders": {}, "threads": {}})

    if folder_ids:
        roots = list(folder_ids)
    else:
        me = client.get("/1/users/current")
        roots = [me.get(k) for k in ("private_folder_id", "desktop_folder_id") if me.get(k)]
        roots += me.get("shared_folder_ids", []) + me.get("group_folder_ids", [])
        if me.get("archive_folder_id"):
            roots.append(me["archive_folder_id"])

    print(f"Walking {len(roots)} root folder(s)...")
    folders, placements = walk_folders(client, roots)
    if include_recent and not folder_ids:
        for tid in recent_thread_ids(client, recent_pages):
            placements.setdefault(tid, None)
    manifest["folders"].update(folders)
    print(f"Found {len(folders)} folder(s), {len(placements)} thread(s).")

    thread_ids = list(placements)[:limit] if limit else list(placements)
    meta = fetch_metadata(client, thread_ids)
    counts = {"exported": 0, "unchanged": 0, "unsupported": 0, "failed": 0}

    for n, tid in enumerate(thread_ids, 1):
        m = meta.get(tid)
        if m is None:
            counts["failed"] += 1
            continue
        entry = manifest["threads"].get(tid, {})
        entry.update(m, folder=placements[tid])
        manifest["threads"][tid] = entry
        prefix = f"[{n}/{len(thread_ids)}] {tid} {m['type']}"

        if m["type"] not in EXPORT_FORMATS:
            entry["status"] = "unsupported"
            counts["unsupported"] += 1
            print(f"{prefix} unsupported, skipped")
            continue
        already = entry.get("status") == "exported" and entry.get("exported_usec") == m["updated_usec"]
        if already and os.path.exists(os.path.join(staging, entry["file"])):
            counts["unchanged"] += 1
            continue
        try:
            entry["file"], entry["format"] = export_thread(client, tid, m["type"], staging)
            entry["status"], entry["exported_usec"] = "exported", m["updated_usec"]
            entry.pop("error", None)
            counts["exported"] += 1
            print(f"{prefix} ok ({entry['format']})")
        except QuipError as e:
            entry["status"], entry["error"] = "failed", str(e)
            counts["failed"] += 1
            print(f"{prefix} FAILED: {e}", file=sys.stderr)
        save_json(manifest_path, manifest)

    save_json(manifest_path, manifest)
    print("Done: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    top, n = write_tree(staging, manifest, top_name or f"Quip import {date.today().isoformat()}")
    print(f"Upload folder ({n} files): {top}")
    return counts


def safe_name(title):
    """A title usable as a macOS file or folder name."""
    name = "".join("-" if c in "/:\\" or ord(c) < 32 else c for c in title).strip().lstrip(".")
    while len(name.encode("utf-8")) > MAX_NAME_BYTES:
        name = name[:-1]
    return name.strip() or "Untitled"


def unique_name(name, ext, taken):
    """Add " (2)", " (3)"... on clashes. Comparison ignores case, like the macOS file system."""
    candidate, n = name + ext, 2
    while candidate.lower() in taken:
        candidate, n = f"{name} ({n}){ext}", n + 1
    taken.add(candidate.lower())
    return candidate


def write_tree(staging, manifest, top_name):
    """Lay exported files out as <top>/<Quip folders>/<title>.<ext> for uploading.

    Files are hard links into staging/files, so the tree takes no extra disk space.
    The tree is rebuilt from scratch each time. Returns (top folder path, file count).
    """
    out = os.path.join(staging, TREE_DIR)
    partial = out + ".partial"
    shutil.rmtree(partial, ignore_errors=True)
    top = os.path.join(partial, safe_name(top_name))
    os.makedirs(top)
    dirs, taken = {}, {}

    def dir_for(fid):
        key = fid or UNFILED
        if key not in dirs:
            if fid is None:
                parent, title = top, UNFILED
            else:
                info = manifest["folders"].get(fid, {"title": fid, "parent": None})
                parent = dir_for(info["parent"]) if info["parent"] else top
                title = info["title"]
            dirs[key] = os.path.join(parent, unique_name(safe_name(title), "", taken.setdefault(parent, set())))
            os.mkdir(dirs[key])
        return dirs[key]

    count = 0
    for tid, e in sorted(manifest["threads"].items(), key=lambda kv: (kv[1].get("title", "").lower(), kv[0])):
        src = os.path.join(staging, e.get("file", ""))
        if e.get("status") != "exported" or not os.path.isfile(src):
            continue
        folder = dir_for(e.get("folder"))
        dest = os.path.join(folder, unique_name(safe_name(e["title"]), "." + e["format"], taken.setdefault(folder, set())))
        try:
            os.link(src, dest)
        except OSError:
            shutil.copy2(src, dest)
        count += 1

    shutil.rmtree(out, ignore_errors=True)
    os.replace(partial, out)
    return os.path.join(out, os.path.basename(top)), count


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--staging", default=DEFAULT_STAGING, help="work folder (default: staging/ next to this script)")
    parser.add_argument("--api-base", default=DEFAULT_API_BASE,
                        help="Quip API host, e.g. https://platform.<company>.onquip.com for some company sites")
    parser.add_argument("--folder", action="append", help="export only this folder ID (repeatable)")
    parser.add_argument("--no-recent", action="store_true", help="skip the recent-threads sweep for unfiled docs")
    parser.add_argument("--limit", type=int, help="export at most N threads (for a trial run)")
    args = parser.parse_args()

    os.umask(0o077)  # staged files readable by your user account only
    client = QuipClient(require_token(), args.api_base)
    counts = run(client, args.staging, args.folder, not args.no_recent, limit=args.limit)
    sys.exit(1 if counts["failed"] else 0)


if __name__ == "__main__":
    main()
