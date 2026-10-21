"""Fail on known vulnerabilities, except triaged packages (allowed.txt).

    python check.py pip-audit report.json
    python check.py trivy report.json
"""
import json
import sys
from pathlib import Path


def version(text):
    return tuple(int(p) if p.isdigit() else 0 for p in text.split(".")[:3])


def allowed():
    rules = {}
    for line in (Path(__file__).parent / "allowed.txt").read_text().splitlines():
        if line.strip() and not line.startswith("#"):
            name, below, *_ = line.split()
            rules[name.lower()] = version(below)
    return rules


def findings(kind, report):
    if kind == "pip-audit":
        for dep in report["dependencies"]:
            for vuln in dep.get("vulns", []):
                yield dep["name"], dep["version"], vuln["id"]
    else:
        for result in report.get("Results", []):
            for vuln in result.get("Vulnerabilities") or []:
                yield vuln["PkgName"], vuln["InstalledVersion"], vuln["VulnerabilityID"]


def main(kind, path):
    rules = allowed()
    failed, triaged = [], []
    for name, installed, vuln_id in findings(kind, json.loads(Path(path).read_text())):
        below = rules.get(name.lower())
        (triaged if below and version(installed) < below else failed).append(f"{name} {installed} {vuln_id}")
    for line in triaged:
        print(f"triaged  {line}")
    for line in failed:
        print(f"FAIL     {line}")
    print(f"{len(failed)} to fix, {len(triaged)} triaged (.github/security/allowed.txt)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
