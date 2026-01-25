import os
import sys
import json
import argparse
import subprocess
import glob
import urllib.request
import tarfile
from collections import defaultdict
import networkx as nx

def get_pkg_info(pkgbuild_path):
    """
    Source the PKGBUILD and extract pkgname, version, depends, and makedepends using JSON output.
    """
    cwd = os.path.dirname(pkgbuild_path)
    if not cwd:
        cwd = '.'
        
    # We constructs a bash command to source the PKGBUILD and output a JSON object.
    # We utilize jq to create the JSON structure safely.
    bash_compat_script = f"""
    source "{os.path.basename(pkgbuild_path)}" || true

    # Create JSON using jq. 
    # bash arrays (pkgname, depends, makedepends) are passed as space-separated strings, then split by jq.
    # We use --arg to safely pass variables.
    
    jq -n \\
      --arg pkgname "${{pkgname[*]}}" \\
      --arg version "${{epoch:+${{epoch}}:}}${{pkgver}}-${{pkgrel}}" \\
      --arg depends "${{depends[*]}}" \\
      --arg makedepends "${{makedepends[*]}}" \\
      '{{
        pkgname: ($pkgname | split(" ") | map(select(length > 0))),
        version: $version,
        depends: ($depends | split(" ") | map(select(length > 0))),
        makedepends: ($makedepends | split(" ") | map(select(length > 0)))
      }}'
    """
    
    cmd = ['bash', '-c', bash_compat_script]
    
    try:
        if not os.path.exists(pkgbuild_path):
            print(f"Skipping {pkgbuild_path}: File not found", file=sys.stderr)
            return None
            
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=cwd)
        # Parse the JSON output directly
        return json.loads(result.stdout)
        
    except subprocess.CalledProcessError as e:
        print(f"Error executing bash script for {pkgbuild_path}: {e}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"Error parsing JSON from {pkgbuild_path}: {e}", file=sys.stderr)
        print(f"Output was: {result.stdout}", file=sys.stderr)
        return None

def build_graph(repo_root):
    G = nx.DiGraph()
    pkg_to_dir = {}
    dir_to_pkgs = defaultdict(list)
    dir_to_ver = {}
    
    # 1. Find all PKGBUILDs
    pkgbuilds = glob.glob(os.path.join(repo_root, '**', 'PKGBUILD'), recursive=True)
    
    # 2. Parse all packages
    dir_deps_map = {}
    
    for pb in pkgbuilds:
        folder = os.path.dirname(pb)
        rel_folder = os.path.relpath(folder, repo_root)
        if rel_folder == '.': continue 
        
        info = get_pkg_info(pb)
        if not info:
            continue
            
        pkgnames = info.get('pkgname', [])
        version = info.get('version', '')
        depends = info.get('depends', [])
        makedepends = info.get('makedepends', [])
        all_deps = set(depends + makedepends)
        
        G.add_node(rel_folder)
        
        dir_to_ver[rel_folder] = version
        dir_deps_map[rel_folder] = all_deps
        
        for p in pkgnames:
            pkg_to_dir[p] = rel_folder
            dir_to_pkgs[rel_folder].append(p)

    # 3. Add edges
    for folder, deps in dir_deps_map.items():
        for dep in deps:
            if dep in pkg_to_dir:
                provider_folder = pkg_to_dir[dep]
                if provider_folder != folder:
                    G.add_edge(provider_folder, folder)
                    
    return G, pkg_to_dir, dir_to_pkgs, dir_to_ver

def download_db(repo_name, output_path):
    repo_slug = os.environ.get('GITHUB_REPOSITORY')
    if not repo_slug:
        print("GITHUB_REPOSITORY not set, assuming local run/test or cannot download.", file=sys.stderr)
        return False
    
    url = f"https://github.com/{repo_slug}/releases/download/repository/{repo_name}.db.tar.gz"
    print(f"Downloading DB from {url}...", file=sys.stderr)
    try:
        urllib.request.urlretrieve(url, output_path)
        return True
    except Exception as e:
        print(f"Failed to download DB: {e}", file=sys.stderr)
        return False

def parse_db(db_path):
    db_pkgs = {}
    if not os.path.exists(db_path):
        return db_pkgs
    
    try:
        with tarfile.open(db_path, "r:gz") as tar:
            for member in tar.getmembers():
                if member.name.endswith("/desc"):
                    f = tar.extractfile(member)
                    if f:
                        content = f.read().decode('utf-8', errors='ignore')
                        lines = content.splitlines()
                        name = None
                        version = None
                        
                        i = 0
                        while i < len(lines):
                            line = lines[i].strip()
                            if line == "%NAME%":
                                name = lines[i+1].strip()
                            elif line == "%VERSION%":
                                version = lines[i+1].strip()
                            i += 1
                        
                        if name and version:
                            db_pkgs[name] = version
    except Exception as e:
        print(f"Error parsing DB: {e}", file=sys.stderr)
        
    return db_pkgs

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--force', action='store_true', help="Rebuild all packages")
    parser.add_argument('--repo-name', help="Name of the repository", required=True)
    args = parser.parse_args()
    
    repo_root = os.getcwd()
    
    # 1. Build Local Graph
    G, pkg_to_dir, dir_to_pkgs, dir_to_ver = build_graph(repo_root)
    all_local_dirs = list(G.nodes())
    
    # 2. Download and Parse Remote DB
    db_file = "current_repo.db.tar.gz"
    if download_db(args.repo_name, db_file):
        remote_pkgs = parse_db(db_file)
    else:
        print("Could not download DB. Assuming empty valid DB (first run?) or network error.", file=sys.stderr)
        remote_pkgs = {}
        
    build_queue = set()
    deletion_queue = set() # This will be pkgnames, not folders
    
    if args.force:
        print("Force rebuild enabled.", file=sys.stderr)
        build_queue = set(all_local_dirs)
    else:
        # 3. Compare Local vs Remote
        
        # Check for updates/adds
        for folder in all_local_dirs:
            local_ver = dir_to_ver.get(folder)
            pkgnames = dir_to_pkgs.get(folder, [])
            
            needs_build = False
            for p in pkgnames:
                remote_ver = remote_pkgs.get(p)
                if remote_ver != local_ver:
                    print(f"Package '{p}' needs build: local '{local_ver}' != remote '{remote_ver}'", file=sys.stderr)
                    needs_build = True
                    break # One mismatch in split pkg triggers rebuild of folder
            
            if needs_build:
                build_queue.add(folder)
        
        # Check for deletions
        # Any package in remote_pkgs that is NOT in any local folder's list
        # We need a set of all local pkgnames
        all_local_pkgnames = set(pkg_to_dir.keys())
        
        for p in remote_pkgs:
            if p not in all_local_pkgnames:
                print(f"Package '{p}' found in DB but not locally. Marking for deletion.", file=sys.stderr)
                deletion_queue.add(p)

        # 4. Dependency Impact Analysis
        # If A changes, and B depends on A, B might need rebuild? 
        # Arch usually handles ABI bumps manually (provides/depends versioning).
        # Simply rebuilding B just because A rebuilt isn't always strictly required unless static linking or ABI break.
        # But commonly in personal repos, we might want to rebuild descendents to be safe.
        # The previous script did it: "Impact Analysis: Find descendents".
        # Let's keep that behavior.
        
        initial_changes = list(build_queue)
        for folder in initial_changes:
            descendants = nx.descendants(G, folder)
            if descendants:
                print(f"Adding descendents of {folder} to build info: {descendants}", file=sys.stderr)
                build_queue.update(descendants)

    # 5. Sort Levels
    subgraph = G.subgraph(build_queue)
    try:
        sorted_nodes = list(nx.topological_sort(subgraph))
    except nx.NetworkXUnfeasible:
        print("Cycle detected in dependency graph!", file=sys.stderr)
        # Fallback to just list
        sorted_nodes = list(build_queue)

    levels = defaultdict(list)
    
    pending = set(sorted_nodes)
    current_level = 1
    
    while pending:
        ready_nodes = []
        for node in pending:
            preds = list(subgraph.predecessors(node))
            if all(p not in pending for p in preds):
                ready_nodes.append(node)
        
        if not ready_nodes and pending:
            break
            
        levels[current_level] = ready_nodes
        for node in ready_nodes:
            pending.remove(node)
        current_level += 1

    output_levels = sorted(levels.keys())
    output_map = {str(k): v for k, v in levels.items()}
    
    outputs = {
        "levels": output_levels,
        "level_map": output_map,
        "deleted": list(deletion_queue)
    }
    
    if os.getenv('GITHUB_OUTPUT'):
        with open(os.getenv('GITHUB_OUTPUT'), 'a') as f:
            f.write(f"levels={json.dumps(output_levels)}\n")
            f.write(f"level_map={json.dumps(output_map)}\n")
            f.write(f"deleted={json.dumps(list(deletion_queue))}\n")
            
    print(json.dumps(outputs, indent=2))

if __name__ == "__main__":
    main()
