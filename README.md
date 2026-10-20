# Autonomi Bitcoin Notary

Cross-platform application (GUI & Headless CLI) that uploads files to the decentralized Autonomi network and anchors their cryptographic storage addresses directly into the Bitcoin blockchain via OpenTimestamps.

## Features
- **Standalone Binary:** Zero dependencies on the target host (no Python, pip, or external `ots` CLI required; bundled with `libxcb-cursor`).
- **Direct OpenTimestamps Integration:** Uses the OTS Python API natively (stamps across 4 calendar servers, verifies Merkle paths directly against Bitcoin block headers).
- **Multi-threaded Architecture:** Native PySide6 interface using `QThread` and structured events; responsive UI with immediate stop capabilities and clean shutdown.
- **Flexible `ant` CLI Discovery:**
  1. Custom path selected in GUI (persisted across sessions)
  2. Application directory
  3. Current working directory
  4. Embedded binary (via `ANT_BUNDLE`)
  5. System `$PATH`
- **Headless Mode:** Full automated pipeline available via terminal with `AutonomiNotary --cli <file>`.

## Verified Testnet / Mainnet Proof
- **Bitcoin Block:** 972281 (2026-10-20 07:22:04 UTC)
- **Final Autonomi Proof Address:** `ant://0f860a7e68ea3ad515fc047c12d3d099c1ec503a83ad073d89820ccf84f66ceb`

## Building from Source (Linux)
```bash
# Standard build
./build_linux.sh

# Build with embedded Autonomi CLI binary
ANT_BUNDLE=~/.local/bin/ant ./build_linux.sh

