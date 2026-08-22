#!/bin/bash
set -ex
pacman-key --init
pacman -Syuq --noconfirm --noprogressbar --ignore linux --ignore linux-firmware --needed

og=$(stat -c '%u:%g' .)
od=$(pwd)
chown -R build: .

if [ -n "${GPG_FILE_PASSWORD}" ]; then
	openssl aes-256-cbc -d -a -pbkdf2 -in "${GPGKEY}" -pass "pass:${GPG_FILE_PASSWORD}" | sudo -u build gpg --import
	unset GPGKEY
	unset GPG_FILE_PASSWORD
fi

if [ -n "${GPG_KEY_PASSWORD}" ]; then
	echo "allow-preset-passphrase" | sudo -u build tee -a /home/build/.gnupg/gpg-agent.conf
	sudo -u build gpg-connect-agent reloadagent /bye
	KGRIPS=$(sudo -u build gpg --list-secret-keys --with-colons --with-keygrip | awk -F: '$1 == "grp" {print $10}')
	for KGRIP in $KGRIPS; do
		echo "${GPG_KEY_PASSWORD}" | sudo -u build /usr/lib/gnupg/gpg-preset-passphrase --preset "${KGRIP}"
	done
	unset GPG_KEY_PASSWORD
fi

cd "$1"
shift

# Import upstream PGP keys if validpgpkeys exists
if [ -f PKGBUILD ] && grep -q 'validpgpkeys' PKGBUILD; then
	mapfile -t pgpkeys < <(bash -c 'source PKGBUILD 2>/dev/null && printf "%s\n" "${validpgpkeys[@]}"')
	if [ ${#pgpkeys[@]} -gt 0 ]; then
		for key in "${pgpkeys[@]}"; do
			[ -z "$key" ] && continue
			echo "Importing PGP key: $key"
			sudo -u build gpg --keyserver keyserver.ubuntu.com --recv-keys "$key" || \
			sudo -u build gpg --keyserver keys.openpgp.org --recv-keys "$key" || \
			echo "Warning: Failed to import key $key"
		done
	fi
fi

if [ -d "_deps" ]; then
	# install in a single transaction so pacman works out the order itself
	mapfile -t -d '' dep_pkgs < <(find _deps -type f -name "*.pkg.tar.zst" -print0 | sort -zu)
	if [ ${#dep_pkgs[@]} -gt 0 ]; then
		pacman -U --noconfirm "${dep_pkgs[@]}"
	fi
fi

sudo -u build --preserve-env=PACKAGER bash -c "$*"

cd "$od"
chown -R "$og" .
