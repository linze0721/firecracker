# shinu v1.18.0 static build

Source: upstream tag `v1.18.0`, commit `eaf83bb718a559d324613d566eaf776b31a62340`, fork branch `shinu/v1.18`. No Firecracker source changes are required. Snapshot format is `12.0.0` (`firecracker --snapshot-version` prints `v12.0.0`).

## Exact build used

Host: x86_64 Void Linux, Rust `1.97.0` from the checked-in `rust-toolchain.toml`, musl target installed, GNU musl cross compiler `/usr/bin/x86_64-linux-musl-gcc` 14.2.1. The host has libseccomp 2.5.5 runtime but no development linker name. The build script uses host libseccomp to compile seccomp filters; this is not a runtime dependency of the resulting binaries.

```sh
# Run from the Firecracker checkout. Install the musl target if absent.
rustup target add --toolchain 1.97.0 x86_64-unknown-linux-musl
mkdir -p /tmp/shinu-p1-build-libs
ln -s /usr/lib/libseccomp.so.2 /tmp/shinu-p1-build-libs/libseccomp.so
LIBRARY_PATH=/tmp/shinu-p1-build-libs \
  CC_x86_64_unknown_linux_musl=x86_64-linux-musl-gcc \
  CARGO_TARGET_X86_64_UNKNOWN_LINUX_MUSL_LINKER=x86_64-linux-musl-gcc \
  cargo build --locked --release --target x86_64-unknown-linux-musl \
    -p firecracker -p jailer
```

If `libseccomp-devel` supplies the linker name, the temporary symlink / `LIBRARY_PATH` are unnecessary. Upstream's container alternative is `tools/devtool -y build --release` (not used for this build). Do not disable seccomp or alter the checked-in filters to get a build through.

Outputs in `build/cargo_target/x86_64-unknown-linux-musl/release/`:

| File | SHA-256 |
| --- | --- |
| `firecracker` | `f1bb8d2b08e457f98fef888cbf01cbf0cce28caf5aabae7ae4b13af0c13d5d7f` |
| `jailer` | `6b57d555128e7a301c949d6c79c0983406b3205ac36ea0fcd076d0a3bb928fd5` |

Both were checked with `file` (static PIE) and Firecracker has no ELF `INTERP` segment. Debug information is retained, not stripped. These hashes identify this build, not a guarantee of byte-identical builds from a different checkout path/toolchain environment.

## Guest kernel

The newest published supported FC-CI kernel is `6.18.62` in this immutable dated artifact set. It is the **build baseline**, not the Docker/LeapCore rollout binary:

```
https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/20261111-778a26c0594c-0/x86_64/vmlinux-6.18.62
SHA256 0ee1eb9074bfe4d90b3a6c0d3ea9c7c7b8c7563e69796fd73e7801db83a67b9f
```

The [official v1.18 prefix listing](https://s3.amazonaws.com/spec.ccfc.min?list-type=2&prefix=firecracker-ci/v1.18/x86_64/) is empty. `tools/devtool:get_newest_s3_artifacts` selects dated prefixes by the newest kernel object modification date. The [official dated listing](https://s3.amazonaws.com/spec.ccfc.min?list-type=2&prefix=firecracker-ci/20261111-778a26c0594c-0/x86_64/) also publishes 6.1.188 and 5.10.271; `tests/framework/artifacts.py` supports 6.18, 6.1 and 5.10. The [kernel config](https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/20261111-778a26c0594c-0/x86_64/vmlinux-6.18.62.config) builds in ACPI, virtio-blk, vsock, ext4, overlayfs, squashfs and balloon, preserving no-initrd boot and LeapCore container filesystem support.

Compatibility review found that this stock CI config omits nft inet/IPv6 tables, nft masquerade, xt comment/mark/multiport, raw tables and other features enabled in 109's existing Docker-compatible 6.1.155 kernel. For 109, build Linux 6.18.62 with shinu's tracked `packaging/kernel-6.18.62.config`, the union of the old netfilter built-ins and the CI baseline. The only removed old symbol is `NF_CT_PROTO_DCCP`, which no longer exists upstream. The custom `6.18.62-shinu` vmlinux SHA-256 is `3f7808d10960bc05a1bfb107587ea1fca1d7810a2993631723637d898c0e82c8`; config SHA-256 is `d3ea50c7afcb808456c9da660de8e71b25e21dd03c2665e18a37073b52aa3ff8`. The source archive `https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-6.18.62.tar.xz` hashes to `490a7aedf1b355c565d3a6c05333ccccb5da82064eca46b590448a7099d7d651`, matching kernel.org's published checksum.

The exact custom kernel commands (Debian trixie container with gcc, make, bc, bison, flex, libssl-dev and libelf-dev installed) are:

```sh
export KCONFIG_CONFIG=/config/kernel-6.18.62.config
export KBUILD_BUILD_USER=shinu KBUILD_BUILD_HOST=shinu
export KBUILD_BUILD_TIMESTAMP="2026-11-15 00:00:00 UTC" KBUILD_BUILD_VERSION=1
make -C /src O=/build LOCALVERSION=-shinu olddefconfig
make -C /src O=/build LOCALVERSION=-shinu -j4 vmlinux
```

Use shinu's `SHINU_KERNEL_FILE` plus mandatory `SHINU_KERNEL_SHA256` to seed the custom kernel, not the stock download. Boot-to-SSH is not sufficient compatibility proof: acceptance includes iptables-nft NAT/mark/comment/multiport rules, Docker bridge connectivity, and every LeapCore container healthy.

Writable virtio-blk drives must explicitly set `"io_engine":"Sync","discard":true`. There is no additional guest CONFIG option for virtio-blk discard: it is a negotiated virtio feature. Ordinary file backends use hole punching, not write-zeroes. See [block-discard.md](api_requests/block-discard.md).

## shinu local asset installation

Package into the same layout shinu's checksum-verifying asset installer accepts:

```sh
out=build/cargo_target/x86_64-unknown-linux-musl/release
stage=$(mktemp -d)
mkdir "$stage/release-v1.18.0-x86_64"
cp "$out/firecracker" "$stage/release-v1.18.0-x86_64/firecracker-v1.18.0-x86_64"
cp "$out/jailer" "$stage/release-v1.18.0-x86_64/jailer-v1.18.0-x86_64"
(cd "$stage/release-v1.18.0-x86_64" && sha256sum firecracker-* jailer-* > SHA256SUMS)
tar -C "$stage" -czf firecracker-v1.18.0-x86_64.tgz release-v1.18.0-x86_64
rm -rf "$stage"
```

Before first v1.18 daemon startup, install this archive as root at `<shinu-root>/cache/firecracker-v1.18.0-x86_64.tgz`, mode 0600, and do not modify a live deployment. shinu verifies both binary digests and creates its asset manifest. Use a maintenance window and cold restart all old Spaces to renegotiate the discard feature; a format-10 memory snapshot cannot be loaded as format 12. Keep old checkpoint disks for cold-boot fallback.
