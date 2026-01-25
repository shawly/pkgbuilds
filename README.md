# PKGBUILDs

Use GitHub Actions for building and packaging a few [AUR](https://aur.archlinux.org) packages and deploy them to [GitHub Releases](https://github.com/shawly/pkgbuilds/releases) so it can be used as a repository in [Arch Linux](https://www.archlinux.org).  Based on [djpohly/PKGBUILD](https://github.com/djpohly/PKGBUILD).


## Using as a pacman repository

To use as custom repository in [Arch Linux](https://www.archlinux.org), add to file `/etc/pacman.conf`:

```
[shawly]
SigLevel = Optional TrustAll
Server = https://github.com/shawly/pkgbuilds/releases/download/repository
```

## Setup Instructions (TODO)

Follow these steps to set up your own PKGBUILDs repository:

### 1. Generate GPG Key

```bash
# Generate a new GPG key for package signing
gpg --full-generate-key
# Select: (1) RSA and RSA
# Keysize: 4096
# Expiration: 0 (does not expire) or set your preferred expiration
# Real name: shawly (Github Actions)
# Email: github-action@users.noreply.github.com
# Comment: Package signing key
# Passphrase: <LEAVE EMPTY> (Hit enter twice. The protection is done via openssl below)
# Note: If you do set a passphrase, you must add it as GPG_KEY_PASSWORD in GitHub Secrets (see step 2).

# List keys to find your key ID
gpg --list-secret-keys --keyid-format=long
# Look for the line starting with 'sec' and note the key ID after 'rsa4096/'

# Export your public key (replace KEY_ID with your actual key ID)
gpg --armor --export KEY_ID > shawly-keyring/public.gpg

# Export your private key for GitHub Actions (replace KEY_ID with your actual key ID)
gpg --armor --export-secret-keys KEY_ID > key.gpg

# Encrypt the private key with a passphrase for GitHub Secrets
# We add -pbkdf2 to fix the deprecation warning and -a to base64 encode it for the workflow
openssl enc -aes-256-cbc -salt -a -pbkdf2 -in key.gpg -out key.gpg.enc
# Enter a strong encryption passphrase and remember it for the GPG_FILE_PASSWORD secret

# Clean up the unencrypted private key
rm key.gpg
```

### 2. Configure GitHub Secrets

Go to your repository settings on GitHub: Settings → Secrets and variables → Actions

Add the following secrets:
- **GPG_FILE_PASSWORD**: The passphrase you used to encrypt key.gpg.enc
- **GPG_KEY_PASSWORD** (optional): The passphrase of your GPG key, if you set one.
- **REPO_TOKEN** (optional): Used only by the automated setup script to manage secrets.
  - **Type**: Fine-grained Personal Access Token
  - **Permissions**: Repository permissions → Secrets (Read and Write)
  - **Note**: You can safely delete this token after the setup workflow completes.

### 3. Update Repository Configuration

The following files have already been updated for shawly:
- ✅ `config.json` - Points to shawly-keyring/public.gpg
- ✅ `shawly-keyring/PKGBUILD` - Package definition
- ✅ `shawly-keyring/shawly-keyring.install` - Install scripts
- ✅ All existing submodules removed

### 4. Add AUR Packages as Submodules

```bash
# Add AUR packages you want to build as git submodules
# Example for adding yay:
git submodule add https://aur.archlinux.org/yay.git yay

# Example for adding other packages:
# git submodule add https://aur.archlinux.org/PACKAGE_NAME.git PACKAGE_NAME

# Commit the changes
git add .gitmodules yay/
git commit -m "Add yay submodule"
git push
```

### 5. Enable Dependabot

Dependabot is already configured in `.github/dependabot.yml`. It will:
- Check for submodule updates weekly
- Automatically create PRs when AUR packages are updated
- Trigger builds when PRs are merged

### 6. Build and Deploy

Once you push changes or merge a dependabot PR:
- GitHub Actions will build the packages
- Sign them with your GPG key
- Deploy to GitHub Releases
- Update the package repository

### 7. Install the Keyring Package (For Users)

Users can install your keyring package to enable package signature verification:

```bash
# Download and install the keyring package
# Replace VERSION with the actual version (e.g., 20260125073557-1)
# You can find the version in the GitHub Releases page
wget https://github.com/shawly/pkgbuilds/releases/download/shawly-keyring/shawly-keyring-VERSION-any.pkg.tar.zst
sudo pacman -U shawly-keyring-*.pkg.tar.zst

# Or add to pacman.conf and install via pacman:
sudo pacman -Sy shawly-keyring
```

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