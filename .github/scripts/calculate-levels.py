import os
import subprocess
import json
import glob
from collections import defaultdict, deque
import re

def get_pkg_info(pkgbuild_path):
    """
    Source the PKGBUILD and extract pkgname, depends, and makedepends.
    """
    cwd = os.path.dirname(pkgbuild_path)
    if not cwd:
        cwd = '.'
        
    cmd = [
        'bash', '-c',
        f'source "{os.path.basename(pkgbuild_path)}" || true; echo "PKGNAME_START"; echo ${{pkgname[@]}}; echo "PKGNAME_END"; echo "DEPS_START"; echo ${{depends[@]}}; echo "DEPS_END"; echo "MAKEDEPS_START"; echo ${{makedepends[@]}}; echo "MAKEDEPS_END"'
    ]
    
    try:
        # Check if file exists
        if not os.path.exists(pkgbuild_path):
            print(f"Skipping {pkgbuild_path}: File not found")
            return None
            
        result = subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=cwd)
        lines = result.stdout.splitlines()
        
        info = {}
        section = None
        for line in lines:
            if line == "PKGNAME_START":
                section = "pkgname"
                continue
            elif line == "PKGNAME_END":
                section = None
                continue
            elif line == "DEPS_START":
                section = "depends"
                continue
            elif line == "DEPS_END":
                section = None
                continue
            elif line == "MAKEDEPS_START":
                section = "makedepends"
                continue
            elif line == "MAKEDEPS_END":
                section = None
                continue
                
            if section == "pkgname":
                info['pkgname'] = line.split()
            elif section == "depends":
                info['depends'] = line.split()
            elif section == "makedepends":
                info['makedepends'] = line.split()
                
        return info
    except subprocess.CalledProcessError as e:
        print(f"Error parsing {pkgbuild_path}: {e}")
        return None

def main():
    repo_root = '.'
    pkgbuilds = glob.glob(os.path.join(repo_root, '**', 'PKGBUILD'), recursive=True)
    
    pkg_to_dir = {}
    dir_to_pkg = {}
    dir_deps = {}
    
    print("Parsing PKGBUILDs...")
    
    for pb in pkgbuilds:
        folder = os.path.basename(os.path.dirname(pb))
        info = get_pkg_info(pb)
        
        if info:
            pkgnames = info.get('pkgname', [])
            depends = info.get('depends', [])
            makedepends = info.get('makedepends', [])
            
            all_deps = set(depends + makedepends)
            
            for p in pkgnames:
                pkg_to_dir[p] = folder
                
            dir_to_pkg[folder] = {
                'pkgnames': pkgnames,
                'deps': all_deps
            }
            
    print("Building dependency graph...")
    dirs = list(dir_to_pkg.keys())
    
    for d in dirs:
        raw_deps = dir_to_pkg[d]['deps']
        internal_deps = set()
        for dep in raw_deps:
            clean_dep = re.split(r'[<>=]', dep)[0]
            
            if clean_dep in pkg_to_dir:
                dep_dir = pkg_to_dir[clean_dep]
                if dep_dir != d: 
                    internal_deps.add(dep_dir)
        
        dir_deps[d] = internal_deps
        
    depth_cache = {}
    visiting = set()
    
    def get_max_depth(u):
        if u in depth_cache:
            return depth_cache[u]
        if u in visiting:
            print(f"Warning: Cycle detected involving {u}")
            return 1 
        
        visiting.add(u)
        
        deps = dir_deps.get(u, set())
        if not deps:
            d = 1
        else:
            d = 1 + max(get_max_depth(dep) for dep in deps)
            
        visiting.remove(u)
        depth_cache[u] = d
        return d

    final_levels = defaultdict(list)
    for d in dirs:
        lvl = get_max_depth(d)
        final_levels[lvl].append(d)
        
    print("Levels determined:")
    max_level = 0
    if final_levels:
        max_level = max(final_levels.keys())
        
    # Output list of levels [1, 2, 3, ...]
    levels_list = sorted(list(final_levels.keys()))
    json_levels = json.dumps(levels_list)
    print(f"Levels: {json_levels}")
    
    # Output map of level -> packages
    level_map = {}
    for i in levels_list:
        level_map[str(i)] = final_levels[i]
    json_map = json.dumps(level_map)
    
    if os.getenv("GITHUB_OUTPUT"):
        with open(os.getenv("GITHUB_OUTPUT"), "a") as f:
            f.write(f"levels={json_levels}\n")
            f.write(f"level_map={json_map}\n")
    else:
        print(f"DEBUG: set-output name=levels::{json_levels}")
        print(f"DEBUG: set-output name=level_map::{json_map}")

if __name__ == "__main__":
    main()
