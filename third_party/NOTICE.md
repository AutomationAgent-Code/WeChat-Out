# Read-only WCDB runtime scanner

`scripts/memory_keys.py` adapts the Windows Config.Cipher layout, XOR mask and
legacy SQLCipher literal recognition from:

- https://github.com/TANGandXUE/wcdb-key-tool
- File: `wcdb_key_tool_windows.py`
- Reviewed revision: `79f1b5b92e12c66aa281b4a60a3c478b5f547dfa`
- Copyright (c) 2025 CloudDreamAI / TANGandXUE
- License: MIT, retained in `wcdb-key-tool.LICENSE`

This project changes process selection, resource limits, credential persistence,
logging, and orchestration. It does not redistribute or execute the upstream CLI,
debugger path, plaintext key cache, or third-party DLLs. Upstream compatibility
claims are not evidence of local end-to-end testing on every WeChat build.
