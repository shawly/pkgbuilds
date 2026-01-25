import os
import subprocess
import glob
import sys
import re
import shutil

def resolve_and_copy_deps(pkgbuild_dir):
    # 1. Ensure we are in a directory with PKGBUILD
    if not os.path.exists(os.path.join(pkgbuild_dir, 'PKGBUILD')):
        print("No PKGBUILD found.")
        sys.exit(1)

    # 2. Get dependencies from PKGBUILD
    print(f"Analyzing {pkgbuild_dir}/PKGBUILD...")
    cmd = [
        'bash', '-c', 
        f'cd "{pkgbuild_dir}" && source PKGBUILD && echo ${{depends[@]}} ${{makedepends[@]}}'
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        initial_deps = res.stdout.strip().split()
    except subprocess.CalledProcessError as e:
        print(f"Error reading PKGBUILD: {e}")
        sys.exit(1)

    # Clean versions (foo>=1.0 -> foo)
    needed = set()
    for d in initial_deps:
        needed.add(re.split('[<>=]', d)[0])
        
    print(f"Direct dependencies: {needed}")

    # 3. Scan available package files in parent directory
    # The python script runs in the workspace root or can resolve to root.
    repo_root = os.path.abspath('.')
    archives = glob.glob(os.path.join(repo_root, '*.pkg.tar.zst'))
    
    if archives:
        print(f"Scanning {len(archives)} archives in {repo_root}...")
        pkg_map = {} # name -> {path, deps}
        
        # We need to map pkg name to file. 
        # Since we are outside the arch container now, we might not have pacman or running in ubuntu-latest runner.
        # Ubuntu runner does not have pacman.
        # But we previously downloaded artifacts which have metadata.
        # Actually, extracting .PKGINFO from .pkg.tar.zst without pacman is tricky but possible with tar/zstd if available.
        # BUT: The previous script ran INSIDE the container which had pacman.
        # This new plan runs OUTSIDE the container? 
        # User says: "Then we need a step which determines the build dependencies (adjust the python script to do that)."
        # If this step runs OUTSIDE, we can't easily iterate dependencies recurisvely because we can't assume pacman exists.
        
        # Alternative: We match by filename convention or simple check?
        # Archives are usually named: name-ver-rel-arch.pkg.tar.zst
        # We can try to parse the filename.
        
        for arc in archives:
            basename = os.path.basename(arc)
            # Simple heuristic: find longest matching package name from 'needed' set?
            # Or parse metadata using built-in tools.
            # tar -I zstd -xOf archive.pkg.tar.zst .PKGINFO 
            
            try:
                # Use zstd and tar to read .PKGINFO
                # Ubuntu runner usually has zstd installed? If not we might fail.
                # Assuming tar supports automagic decompression or we pipe.
                 
                # Let's try explicit pipe if available, else just try tar
                
                cmd_info = f"tar --use-compress-program=zstd -xOf '{arc}' .PKGINFO"
                res_info = subprocess.run(cmd_info, shell=True, capture_output=True, text=True)
                
                if res_info.returncode != 0:
                   # Try -J for xz or -z for gzip? No, it's zst.
                   # If zst not available, we can't read.
                   # Fallback: Parse filename. 
                   # e.g. "qtutilities-qt6-6.19.0-1-x86_64.pkg.tar.zst"
                   # It is imprecise but better than nothing if no tools.
                   pass
                
                name = None
                provides = []
                
                if res_info.returncode == 0:
                    for line in res_info.stdout.splitlines():
                        if line.startswith('pkgname = '):
                            name = line.split('=')[1].strip()
                        elif line.startswith('provides = '):
                            provides.append(line.split('=')[1].strip())
                            
                # If we couldn't get name from .PKGINFO, try filename parsing or skip?
                # We really need precise matching.
                
                if name:
                    pkg_map[name] = arc
                    for p in provides:
                         pkg_map[re.split('[<>=]', p)[0]] = arc

            except Exception as e:
                print(f"Failed to inspect {arc}: {e}")

        # 4. Resolve Direct Dependencies (Single Level for now, or just map 'needed')
        # Since we are outside and might not be able to read deep dependencies (recursive) easily without a full pacman DB...
        # Wait, if we provided previously built packages, we assume the environment is clean except for what we provide.
        # If A depends on B, and we build A. B must be provided.
        # If B depends on C. C must be provided too?
        # Technically yes.
        # But `needed` only contains direct dependencies of A.
        # If B is installed, pacman will check its deps.
        # If libraries of C are needed by B at runtime (and thus link time for A), then C is needed.
        # So we probably need recursive closure.
        # But we can't easily get dependencies of B without reading B's metadata again.
        # We DID read B's metadata above (.PKGINFO contains `depend = ...`).
        
        # Let's parse deps from .PKGINFO too.
        
        deps_map = {} # name -> list of deps
        
        for arc in archives:
            # ... (Redo loop with deps parsing)
             cmd_info = f"tar --use-compress-program=zstd -xOf '{arc}' .PKGINFO"
             res_info = subprocess.run(cmd_info, shell=True, capture_output=True, text=True)
             
             if res_info.returncode == 0:
                name = None
                pkg_deps = []
                pkg_provides = []
                
                for line in res_info.stdout.splitlines():
                    if line.startswith('pkgname = '):
                        name = line.split('=')[1].strip()
                    elif line.startswith('depend = '):
                        d = line.split('=')[1].strip()
                        pkg_deps.append(re.split('[<>=]', d)[0])
                    elif line.startswith('provides = '):
                        p = line.split('=')[1].strip()
                        pkg_provides.append(re.split('[<>=]', p)[0])

                if name:
                    pkg_map[name] = arc
                    deps_map[name] = pkg_deps
                    for p in pkg_provides:
                        pkg_map[p] = arc
                        deps_map[p] = pkg_deps

        
        # Solve closure
        deps_folder = os.path.join(pkgbuild_dir, '_deps')
        os.makedirs(deps_folder, exist_ok=True)
        
        to_install = set()
        queue = list(needed)
        seen = set()
        
        while queue:
            req = queue.pop(0)
            if req in seen:
                continue
            seen.add(req)
            
            if req in pkg_map:
                arc_path = pkg_map[req]
                to_install.add(arc_path)
                
                # Check transitive
                # We need to map the req to the actual package name (primary key) to look up deps
                # But pkg_map points to path. 
                # We need path -> deps
                # Let's map path -> deps (reverse lookup or store better)
                # Re-optimization:
                # pkg_data = {path: ..., deps: ...}
                pass
                
        # Re-RE-implementing simply:
        
        # Build a full map first
        meta_map = {} # name -> {path, deps}
        
        for arc in archives:
             cmd_info = f"tar --use-compress-program=zstd -xOf '{arc}' .PKGINFO"
             res_info = subprocess.run(cmd_info, shell=True, capture_output=True, text=True)
             if res_info.returncode == 0:
                name = None
                local_deps = []
                provides = []
                for line in res_info.stdout.splitlines():
                    if line.startswith('pkgname = '):
                        name = line.split('=')[1].strip()
                    elif line.startswith('depend = '):
                        d = line.split('=')[1].strip()
                        local_deps.append(re.split('[<>=]', d)[0])
                    elif line.startswith('provides = '):
                        p = line.split('=')[1].strip()
                        provides.append(re.split('[<>=]', p)[0])
                
                if name:
                    entry = {'path': arc, 'deps': local_deps}
                    meta_map[name] = entry
                    for p in provides:
                        meta_map[p] = entry # Point alias to same entry
        
        queue = list(needed)
        resolved_files = set()
        checked = set()
        
        while queue:
            n = queue.pop(0)
            if n in checked:
                continue
            checked.add(n)
            
            if n in meta_map:
                entry = meta_map[n]
                if entry['path'] not in resolved_files:
                    resolved_files.add(entry['path'])
                    print(f"  Found dependency: {n} -> {os.path.basename(entry['path'])}")
                    # Add its deps to queue
                    queue.extend(entry['deps'])

        # Copy files
        if resolved_files:
            print(f"Copying {len(resolved_files)} dependencies to {deps_folder}...")
            for f in resolved_files:
                shutil.copy(f, deps_folder)
        else:
            print("No local dependencies found to copy.")

    else:
        print("No archives found in workspace.")

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: install-deps.py <pkgbuild_dir>")
        sys.exit(1)
    resolve_and_copy_deps(sys.argv[1])
