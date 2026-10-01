# Optional Rust policy scanner

This companion extension implements `PolicyScanner.scan(plan, request)` for Linux
native isolation. The companion now also includes a Linux/macOS native filesystem
backend in `rust/src/filesystem/`, described in [the build guide](../README.md).
The application's Python scanner remains the default. Text matching stays in Python; directory enumeration, path opening and batched metadata
for native file tools on both platforms can use Rust under the scoped `DirectoryReader` contract.
Linux workspace preflight remains part of the policy scanner; it does not use the
macOS-only hard-link preflight entry point.

## Build and select

The crate and Python packaging root is `rust/`. The package is `rust-backend`
and the imported module is `rust_backend`; the Linux scanner requires
`API_VERSION=1`. Native file tools also require `FILESYSTEM_API_VERSION=2`
(current extension version 0.3.0). Install the wheel into the Agent's Python,
not only the task project's environment.

Build commands, target platforms, offline dependencies, source installation,
release wheel selection and integrity checks are maintained in the
[shared build guide](../README.md). Source installation attempts an optional
build after committing the core installation. Release installation only uses a
precompiled wheel if present; it never runs a Rust compiler on the user's machine.

`AGENT_NATIVE_SCANNER=python` is the default. After installing a compatible
extension, set `AGENT_NATIVE_SCANNER=rust` and restart native mode. This selects
Linux policy scanning and the native filesystem backend; on macOS the same setting
selects filesystem operations and workspace preflight, while Seatbelt rules remain
unchanged. Local and Docker modes do not use this native backend.

Explicit Rust selection never silently falls back after missing imports,
incompatible APIs or scan failures. Configuration is selected once per backend
instance. Installation does not change the setting; an existing explicit `rust`
selection must be repaired or changed to `python` if extension installation fails.
No runtime compilation or executable lookup in the scanned workspace occurs.

## Semantics

- Every call owns fresh directory facts, identities and metrics. Static plans may
  be reused; filesystem observations never survive a call.
- Directory enumeration uses `open(O_DIRECTORY | O_NOFOLLOW)` plus `fdopendir` /
  `readdir`. Unknown entry types use `fstatat(AT_SYMLINK_NOFOLLOW)` while the fd is
  alive. Descriptors close through Rust RAII, including cancellation and failures.
- Protected names, lexical paths, aliases, unreadable system subtrees, masked
  workspace validation, root identity and mount snapshot revalidation follow the
  Python implementation. The scanner is not an atomic filesystem snapshot.
- Paths cross the boundary as filesystem bytes, without lossy Unicode conversion.
  ASCII matching stays in Rust. Non-ASCII names use the active Python interpreter's
  built-in `str.lower()` with surrogateescape, preserving its Unicode version and
  contextual sigma rules. This is an intentional compatibility path, not an
  application-supplied per-file callback.
- Rust releases the GIL during scans. It periodically reacquires it for signals
  and the existing cancellation context (roughly every 10 ms when making progress).
  Blocking filesystem operations cannot promise a hard cancellation deadline.
- Public masks, Git paths, failure classes/errno/path and metric fields match the
  Python contract. Timings naturally differ; DT_UNKNOWN classification time is
  included in enumeration rather than the Python classification subphase.

## Verify

Run the import checks, Rust unit tests and native file contracts in the
[shared verification guide](../README.md#验证). For policy-specific regression
and measurement:

```sh
python -m pytest -q tests/test_rust_policy_scan.py tests/test_policy_scan_contract.py tests/test_linux_policy_scan.py tests/test_linux_merged_preflight.py
python scripts/benchmark_policy_backends.py --files 20000 --repeats 7
python scripts/benchmark_policy_backends.py --workspace /path/to/project --read-path /path/to/conda
```

Use an unprivileged Linux account to test permission-denied behavior. The default
Python suite skips differential cases when the companion is absent; CI imports
and verifies the extension before running them. Filesystems that reject undecodable
filenames skip that filesystem case; in-memory Rust tests still cover byte-preserving
matching and ordering. A benchmark does not replace real native isolation tests.

Binding reference: [PyO3 parallelism and GIL release](https://pyo3.rs/v0.27.2/parallelism).
