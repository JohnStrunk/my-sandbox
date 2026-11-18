# Fedora 44 container preflight for the shared non-VM provisioning inputs.
# This is a package/tool compatibility check, not a devbox runtime: it does not
# start Lima, systemd, KVM, mounts, or the full VM provisioner.
#
# Pin the Fedora 44 multi-arch index digest. Keep its release aligned with the
# Fedora 44 cloud images in lima/devbox.yaml.
FROM fedora:44@sha256:15da97ed9aa94dd3d92c3925eb89520f6c80e07ea7ee24cba848d1f8b6912b3e

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
