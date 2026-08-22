# PKGBUILDs

Use GitHub Actions for building and packaging a few [AUR](https://aur.archlinux.org) packages and deploy them to [GitHub Releases](https://github.com/shawly/pkgbuilds/releases) so it can be used as a repository in [Arch Linux](https://www.archlinux.org).  Based on [djpohly/PKGBUILD](https://github.com/djpohly/PKGBUILD).


## Using as a pacman repository

Import the keyring:

```bash
curl -s https://api.github.com/repos/shawly/pkgbuilds/releases/tags/repository \
  | grep -o 'https://[^"]*shawly-keyring-[^"]*\.pkg\.tar\.zst' \
  | head -1 | xargs curl -LO
sudo pacman -U shawly-keyring-*.pkg.tar.zst
```

The keyring is rebuilt whenever the signing key changes, so the version in the
filename moves; resolving it from the release avoids a stale URL.

To use as custom repository in [Arch Linux](https://www.archlinux.org), add to file `/etc/pacman.conf`:

```
[shawly]
SigLevel = Required DatabaseOptional
Server = https://github.com/shawly/pkgbuilds/releases/download/repository
```

`Required` means pacman refuses any package whose signature does not verify
against the key from `shawly-keyring`, so import the keyring first. The older
`SigLevel = Optional TrustAll` accepted anything the server handed out, which
threw away the only thing the signing pipeline produces. If pacman starts
rejecting packages after switching, the keyring is missing or not trusted:

```bash
pacman-key --list-keys | grep -i shawly
```

Every published package also carries a Sigstore build-provenance attestation, binding
it to the exact workflow run, commit, and runner that built it. This is independent of
the GPG signature above and does not require trusting the signing key at all:

```bash
gh attestation verify shawly-keyring-*.pkg.tar.zst -R shawly/pkgbuilds
```

## Fork Instructions

Follow these steps to set up your own PKGBUILDs repository:

### Configure GitHub Secrets

Go to your repository settings on GitHub: Settings → Secrets and variables → Actions

Add the following secrets:
- **GPG_FILE_PASSWORD**: The passphrase you used to encrypt key.gpg.enc
- **GPG_KEY_PASSWORD** (optional): The passphrase of your GPG key, if you set one.
- **REPO_TOKEN** (optional): Used only by the automated setup script to manage secrets.
  - **Type**: Fine-grained Personal Access Token
  - **Permissions**: Repository permissions → Secrets (Read and Write)
  - **Note**: You can safely delete this token after the setup workflow completes.

If you use only the REPO_TOKEN you can skip the other variables as the setup workflow will setup all keys automatically.

### Add AUR Packages as Submodules

```bash
# Add AUR packages you want to build as git submodules
# Example for adding yay:
git submodule add https://aur.archlinux.org/yay.git yay

# Example for adding other packages:
# git submodule add https://aur.archlinux.org/PACKAGE_NAME.git PACKAGE_NAME

# Commit the changes
git add .gitmodules yay/
git commit -m "feat: add yay submodule"
git push
```

### Build and Deploy

Once you push changes or merge a dependabot PR:
- GitHub Actions will build the packages
- Sign them with your GPG key
- Deploy to GitHub Releases
- Update the package repository

## Customizing

To build AUR packages of your own selection, fork this repository.  The master branch contains most of the build actions.

  - Fork this GitHub repository.
  - Follow the Setup Instructions above
  - Add git submodules for the AUR packages you want
  - Each time dependabot finds a submodule update, the package will be built and uploaded, and the repository updated.

## Patching a submodule's PKGBUILD

Submodules track their AUR repo directly, so a local edit to a checked-out
PKGBUILD doesn't survive the next `git submodule update` and there's usually
no push access to commit the fix upstream. For a small local change (e.g. a
build option), add a patch instead:

```bash
cd kodi-git
$EDITOR PKGBUILD                 # make the change
git diff > ../patches/kodi-git/0001-renderer-gles.patch
git checkout -- .                # leave the submodule clean, still tracking AUR
```

`patches/<pkgname>/*.patch` files are applied (in filename order, via
`patch -p1`) to that submodule's working tree on every CI checkout, right
before the PKGBUILD is read — see `.github/scripts/apply-patches.sh`. A
package with no `patches/<pkgname>/` directory is unaffected. If an AUR
update makes a patch stop applying, CI fails loudly on that package instead
of silently building unpatched; regenerate the patch the same way.

## config.json default values (all values are optional)
```json
{
    "enc_gpg": "key.gpg.enc",
    "pub_gpg": "public.gpg",
    "name": "GitHub Action",
    "email": "github-action@users.noreply.github.com",
    "repo_name": "${{ github.repository_owner }}"
}
```