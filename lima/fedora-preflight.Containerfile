# Fedora 44 container preflight for the shared non-VM provisioning inputs.
# This is a package/tool compatibility check, not a devbox runtime: it does not
# start Lima, systemd, KVM, mounts, or the full VM provisioner.
#
# Pin the Fedora 44 multi-arch index digest. Keep its release aligned with the
# Fedora 44 cloud images in lima/devbox.yaml.
FROM fedora:44@sha256:43b29f65a41eb9c35e1cd5323e3bdf3b655c2357a9f4f1ff2f9c2798e5045d80

COPY lima/fedora-packages.sh /opt/devbox/fedora-packages.sh
COPY lima/tool-versions.json /opt/devbox/tool-versions.json
COPY scripts/fedora-preflight-container.sh /opt/devbox/fedora-preflight-container.sh
COPY scripts/fedora-preflight-runtime.sh /opt/devbox/fedora-preflight-runtime.sh

# Cache the expensive package and tool installs. The entry point below always
# refreshes metadata and re-resolves pins, so cached layers cannot hide a
# package that has disappeared from the live repositories.
RUN chmod 0755 \
    /opt/devbox/fedora-preflight-container.sh \
    /opt/devbox/fedora-preflight-runtime.sh \
  && /opt/devbox/fedora-preflight-container.sh

ENTRYPOINT ["/opt/devbox/fedora-preflight-runtime.sh"]
