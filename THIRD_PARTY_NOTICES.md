# Third-party notices

This repository vendors the Windows entry point of
[`wcdb-key-tool`](https://github.com/NinjaSln-labs/wxlocal), used to obtain
verified WCDB/SQLCipher keys from the currently logged-in user's local Weixin
process.

- Copyright © 2025 CloudDreamAI / TANGandXUE
- License: MIT
- Vendored files: `vendor/wcdb-key-tool/`

The upstream license and README are preserved next to the vendored source.
Python packages installed from `requirements.txt` are not vendored and retain
their respective licenses.

The default local embedding model is
[`BAAI/bge-small-zh-v1.5`](https://huggingface.co/BAAI/bge-small-zh-v1.5)
(MIT). Inference uses `fastembed` (Apache-2.0) and the Enterprise WeChat long
connection uses `wecom-aibot-sdk-python` (MIT). Model weights and Python
packages are downloaded during local setup and are not committed to this
repository.
