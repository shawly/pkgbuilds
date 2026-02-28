FROM archlinux:base-devel

COPY entrypoint.sh /entrypoint.sh

RUN pacman -Syuq \
      --noconfirm \
      --noprogressbar \
      --ignore linux \
      --ignore linux-firmware \
      --needed \
      base-devel \
      devtools \
      dbus \
      sudo \
      jq \
    && rm -rf /var/cache/pacman/pkg/* /var/lib/pacman/sync/* \
    && dbus-uuidgen --ensure=/etc/machine-id \
    && groupadd -f -r wheel \
    && useradd -m -G wheel -s /bin/bash build \
    && (echo "%wheel ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/wheel) \
    && chmod +x entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]