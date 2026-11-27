# Third-Party Notices

This repository bundles or integrates third-party open-source tools and offline runtime packages under `vendor/`. Each component remains subject to its own upstream license terms.

## 1. `vendor/qq-key/`
- **Upstream**: `QQBackup/qq-win-db-key`
- **License**: 非商用开源协议 第1版 / Non-Commercial Open Source License (based on CC BY-NC-SA 4.0); see [`vendor/qq-key/LICENSE`](vendor/qq-key/LICENSE) and [`vendor/qq-key/README.md`](vendor/qq-key/README.md).
- **Important**: Commercial use or redistribution violating `vendor/qq-key/LICENSE` is prohibited unless separately authorized by the upstream copyright holder.

## 2. `vendor/python/`
- **Component**: Official Python Embeddable Distribution for Windows (`python-3.13.17-embed-amd64.zip`)
- **Upstream**: Python Software Foundation (`https://www.python.org/`)
- **License**: Python Software Foundation License Version 2 (`PSF-2.0`)

## 3. `vendor/wheels/`
Prebuilt Windows x64 wheels bundled for offline runtime provisioning:
- **`sqlcipher3-wheels`** (`0.5.7`): MIT / BSD-style upstream packaging with SQLCipher (BSD-3-Clause)
- **`pycryptodome`** (`4.2.0`): BSD-2-Clause / Public Domain
- **`zstandard`** (`0.25.0`): BSD-3-Clause
- **`frida`** (`16.7.19`): wxWindows Library Licence 3.1 (LGPL-2.1 with linking exception)
