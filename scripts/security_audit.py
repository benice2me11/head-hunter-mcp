"""Check the selected Python environment against release-specific PyPI advisories.

Only public package names and versions are sent. No env, cookies or user files
are read. RECORD integrity is a consistency check, not an upstream attestation.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from importlib.metadata import distributions
from pathlib import Path
from urllib.request import Request, urlopen
import base64
import hashlib
import json
import sys


def inspect_release(item):
    name, version = item
    url = f"https://pypi.org/pypi/{name}/{version}/json"
    try:
        with urlopen(Request(url, headers={"User-Agent": "work-search-dependency-audit/1.0"}), timeout=30) as response:
            data = json.load(response)
        return {"name": name, "version": version, "checked": True,
                "yanked": data["info"].get("yanked", False),
                "vulnerabilities": data.get("vulnerabilities", [])}
    except Exception as error:
        return {"name": name, "version": version, "checked": False, "error_type": type(error).__name__}


def main():
    installed = {}
    for dist in distributions():
        name = dist.metadata["Name"]
        if name.lower().replace("_", "-") != "hh-mcp-server":
            installed[(name, dist.version)] = dist
    results = list(ThreadPoolExecutor(max_workers=8).map(inspect_release, sorted(installed)))
    mismatches = []
    checked_files = 0
    for (name, version), dist in installed.items():
        for entry in dist.files or []:
            if not entry.hash or entry.hash.mode != "sha256":
                continue
            path = Path(dist.locate_file(entry))
            if not path.is_file():
                mismatches.append({"package": name, "file": str(entry), "issue": "missing"})
                continue
            checked_files += 1
            digest = base64.urlsafe_b64encode(hashlib.sha256(path.read_bytes()).digest()).rstrip(b"=").decode()
            if digest != entry.hash.value:
                mismatches.append({"package": name, "file": str(entry), "issue": "hash_mismatch"})
    report = {"checked_at": datetime.now(timezone.utc).isoformat(),
              "environment": {"python": sys.version.split()[0], "platform": sys.platform}, "package_count": len(results),
              "packages": results, "record_checked_files": checked_files,
              "record_issues": mismatches}
    target = Path(sys.argv[1])
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    findings = [{"name": r["name"], "version": r["version"], "checked": r["checked"],
                 "yanked": r.get("yanked"), "advisories": [v["id"] for v in r.get("vulnerabilities", [])]}
                for r in results if r.get("vulnerabilities") or r.get("yanked") or not r["checked"]]
    print(json.dumps({"report": str(target), "packages": len(results),
                      "findings": findings, "record_checked_files": checked_files,
                      "record_issue_count": len(mismatches),
                      "record_issue_examples": mismatches[:5]}, ensure_ascii=False))
    raise SystemExit(1 if findings or mismatches else 0)


if __name__ == "__main__":
    main()
