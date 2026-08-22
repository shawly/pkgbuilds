"""
Thin client for the aurweb RPC v5 info endpoint.

https://wiki.archlinux.org/title/Aurweb_RPC_interface

Used by audit_submodule.py to check a submodule's claimed maintainer and
commit history against what AUR itself says, independent of what the git
submodule pointer or PKGBUILD claims.
"""

import json
import sys
import urllib.error
import urllib.parse
import urllib.request

RPC_URL = "https://aur.archlinux.org/rpc/v5/info"
_TIMEOUT = 20


def get_info(pkgbases):
    """
    Batch-lookup AUR package info for a list of pkgbase/pkgname strings.

    Returns {name: info_dict_or_None}. A None value means AUR has no record
    of that name at all (e.g. it was deleted, or the RPC call failed).
    """
    pkgbases = list(dict.fromkeys(pkgbases))  # de-dupe, preserve order
    if not pkgbases:
        return {}

    query = urllib.parse.urlencode([("v", "5")] + [("arg[]", p) for p in pkgbases])
    url = f"{RPC_URL}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "shawly-pkgbuilds-audit/1.0"})

    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.load(resp)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        print(f"AUR RPC request failed: {exc}", file=sys.stderr)
        return {name: None for name in pkgbases}

    if data.get("type") == "error":
        print(f"AUR RPC returned an error: {data.get('error')}", file=sys.stderr)
        return {name: None for name in pkgbases}

    results = data.get("results", [])

    by_name = {}
    by_base = {}
    for result in results:
        if result.get("Name"):
            by_name[result["Name"]] = result
        if result.get("PackageBase"):
            by_base.setdefault(result["PackageBase"], result)

    return {name: by_name.get(name) or by_base.get(name) for name in pkgbases}
