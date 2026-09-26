# Verified Stockfish artifacts

Build 0.5.0b3 pins both archive and executable SHA-256 values. New downloads are streamed in 1 MiB chunks under the exact expected byte limit, staged on the same filesystem, and checked before installation. Only the named regular binary member is copied; archive paths are never extracted into the configuration tree. Installation uses `os.replace`, so a failed download or validation keeps the previous executable intact. Concurrent boards sharing a binary directory share an installation lock.

New glibc x86 installations use the upstream baseline build, without assuming AVX2. An existing official AVX2 binary is accepted only when its executable digest matches the pinned release. The selected platform's cached executable must have a known size and digest before launch. Linux ARM64 with musl uses Alpine's package. Stockfish 18 does not publish the integration's former `stockfish-ubuntu-arm64.tar` URL; glibc ARM64 now reports no verified package rather than attempting that URL. Other platforms also report unavailability explicitly.

`Check chess engine` in Board & settings validates readiness, prepares a verified engine if needed, and pings its process. A failed cached process is replaced once. The check retries a previous installation/spawn failure, is bounded to 150 seconds, and rejects active games. Downloads are bounded to 120 seconds. Review allows 150 seconds for first-position startup and 15 seconds subsequently. These controls never command the chessboard or speech.

## Acquisition and validation — September 5, 2026

- [Stockfish 18 official release](https://github.com/official-stockfish/Stockfish/releases/tag/sf_18): both glibc archive hashes match the GitHub release API's published asset digests. Executable hashes were computed from those matching archives.
- [Alpine x86 package](https://pkgs.alpinelinux.org/package/edge/testing/x86_64/stockfish) and [ARM64 package](https://pkgs.alpinelinux.org/package/edge/testing/aarch64/stockfish): archives were retrieved from the publisher's HTTPS repository and verified with `apk verify`. X86 verified with the HA add-on's existing Alpine key; ARM64 verified using the publisher's [616ae350 public key](https://alpinelinux.org/keys/alpine-devel@lists.alpinelinux.org-616ae350.rsa.pub) in a temporary verification directory. System trust settings were not changed. SHA-256 digests below pin those signed package bytes; runtime uses these pinned digests rather than invoking apk.
- The real installer verified and extracted all four pinned archives in an isolated directory. The reference installation's executable matches the x86 Alpine digest. Only that platform has received a live engine execution check; archive validation alone is not cross-platform runtime qualification.

### stockfish-ubuntu-x86-64-avx2.tar (official glibc x86)

- Source: [stockfish-ubuntu-x86-64-avx2.tar](https://github.com/official-stockfish/Stockfish/releases/download/sf_18/stockfish-ubuntu-x86-64-avx2.tar)
- Archive: `536c0c2c0cf06450df0bfb5e876ef0d3119950703a8f143627f990c7b5417964` (114,401,280 bytes)
- Executable `stockfish/stockfish-ubuntu-x86-64-avx2`: `6b087694916228c905a5e14db74cca8c7e5643602226af1fa5d42353c455b9f9` (112,933,248 bytes)

### stockfish-ubuntu-x86-64.tar (official glibc x86)

- Source: [stockfish-ubuntu-x86-64.tar](https://github.com/official-stockfish/Stockfish/releases/download/sf_18/stockfish-ubuntu-x86-64.tar)
- Archive: `5c6f38b02a4da5f3ffe763f27da6c3e743eebefd92b50cb3661623b96696adff` (114,391,040 bytes)
- Executable `stockfish/stockfish-ubuntu-x86-64`: `7a44d64fd877ee888a5160349827563444e1935ca6c1095d0f8e0859d57101c7` (112,920,960 bytes)

### stockfish-18-r0.apk (Alpine x86)

- Source: [stockfish-18-r0.apk](https://dl-cdn.alpinelinux.org/alpine/edge/testing/x86_64/stockfish-18-r0.apk)
- Archive: `804a4ae7d35ed55d30dd031e6e1c738a4d9d3e2cbe2c293495c12ec4c67271e1` (75,884,923 bytes)
- Executable `usr/bin/stockfish`: `f641c102b2e46682a24284566e7bf1222600da594106f29d901e24b0460aeac9` (112,982,200 bytes)

### stockfish-18-r0.apk (Alpine ARM64)

- Source: [stockfish-18-r0.apk](https://dl-cdn.alpinelinux.org/alpine/edge/testing/aarch64/stockfish-18-r0.apk)
- Archive: `be308a62ea3045a9e36b2336ebad7a6e38b9030285b1c7e4927249e97b21b9b4` (75,887,859 bytes)
- Executable `usr/bin/stockfish`: `cc28730bcc22f1e510e82561846bef0aa5bddb92cd83441f1dc5d034a0cb3f8a` (112,986,200 bytes)

## Maintenance

An upstream version or byte change requires an explicit manifest update backed by publisher verification and tests. A moved/removed Alpine edge package fails visibly; do not bypass its digest check or fall back to an arbitrary executable. Downloaded binaries remain cached in the HA configuration and are not included in the integration ZIP.
