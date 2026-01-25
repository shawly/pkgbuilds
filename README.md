# PKGBUILDs

Use GitHub Actions for building and packaging a few [AUR](https://aur.archlinux.org) packages and deploy them to [GitHub Releases](https://github.com/shawly/pkgbuilds/releases) so it can be used as a repository in [Arch Linux](https://www.archlinux.org).  Based on [djpohly/PKGBUILD](https://github.com/djpohly/PKGBUILD).


## Using as a pacman repository

Import the keyring:

```bash
wget https://github.com/shawly/pkgbuilds/releases/download/repository/shawly-keyring-20260125000000-1-any.pkg.tar.zst
sudo pacman -U shawly-keyring-*.pkg.tar.zst
```

To use as custom repository in [Arch Linux](https://www.archlinux.org), add to file `/etc/pacman.conf`:

```
[shawly]
SigLevel = Optional TrustAll
Server = https://github.com/shawly/pkgbuilds/releases/download/repository
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