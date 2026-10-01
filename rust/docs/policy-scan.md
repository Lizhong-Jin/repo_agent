# Optional Rust policy scanner

This companion extension implements `PolicyScanner.scan(plan, request)` for Linux
native isolation. The companion now also includes a macOS native filesystem
backend in `rust/src/filesystem/`, described in [the build guide](../README.md).
The application's Python scanner remains the default. Text matching stays in Python; macOS directory enumeration and path opening
can use Rust under the existing scoped `DirectoryReader` contract.

## Build and select

The crate and Python packaging root is `rust/`: `Cargo.toml`, `Cargo.lock`,
`build.rs`, and `pyproject.toml` live there. The shared crate entry is `src/lib.rs`; scanner bindings and implementation
live in `src/policy_scan/`, and shared errors live in `src/error.rs`.

Source installation automatically attempts to build and install this extension
after the core installation commits. It requires Rust >= 1.85 (`cargo` and
`rustc`), a C linker, and Python >= 3.11. The installer does not install a Rust
toolchain. Missing tools, failed compilation, or cancelling the optional step
produce a warning and leave the core installation usable with the default Python
scanner. Build dependencies are isolated from the Agent environment; Cargo output
uses a temporary directory outside the workspace. Offline builds require cached
Cargo dependencies and local maturin wheels supplied through `--wheelhouse`.

Release installation uses the precompiled wheel recorded in `release.json`, checks
its hash, installs it without dependencies or network access, and verifies its API.
It never invokes a compiler. Missing or unloadable optional wheels do not undo the
core installation. Legacy releases without a wheel remain supported.

Shared build entry points and offline options are documented in [rust/README.md](../README.md).
Final artifacts are retained in `rust_wheels/<version>/`; temporary Cargo output is cleaned.

To build manually from the repository root:

```sh
python scripts/build_rust.py build --target host
python scripts/build_rust.py wheel --target host
# Use the exact wheel path printed above, with the Agent's Python interpreter:
python -m pip install --no-deps rust_wheels/<version>/<generated-wheel-filename>.whl
export AGENT_NATIVE_SCANNER=rust
```

`AGENT_NATIVE_SCANNER=python` selects the reference implementation. Missing,
incompatible, or failed Rust scans cause an error; they never silently switch to
Python. Configuration is selected once per backend instance. No runtime build or
executable lookup in the scanned workspace occurs.

Use an external build directory: Cargo can create hardlinked artifacts, which the
existing native workspace validator deliberately rejects. Do not relax workspace
validation or exclude build trees to work around this.

The extension is a separate platform wheel (`rust-backend`), with PyO3
abi3 for CPython >= 3.11. Install it into the Agent's own Python environment.
The main application's universal wheel remains separate from the native binary.
Source archives include this directory. Do not copy a macOS wheel to WSL/Linux.

The release builder accepts `--rust-wheelhouse DIR` (also searches `--wheelhouse`
when omitted), then the repository `rust_wheels/` directory, selects wheels by extension version, ABI and target platform, and
embeds them alongside the main wheel with integrity hashes. If no matching wheel
exists, it attempts a build only for the build machine's own platform. Other
targets require prebuilt wheels; there is no implicit cross compilation. Missing
compatible wheels normally emit warnings. `--require-rust` makes them fatal for
Linux/macOS targets before any release archives are replaced. Windows is excluded:
this scanner is currently POSIX-only.

`.github/workflows/policy-scan-wheels.yml` builds and tests companion artifacts for
Linux/macOS x86_64 and arm64. Linux uses manylinux 2.28; macOS deployment floors are
10.15 (x86_64) and 11.0 (arm64). Download the four workflow artifacts into one
wheel directory for multi-platform release builds. The workflow does not publish
to PyPI or create a release automatically.

Installing the extension does not change `AGENT_NATIVE_SCANNER`: Python remains
the default. An explicit `rust` setting is retained on reinstall; if optional
installation fails, select `python` or repair the extension before using it.

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

```sh
python -m pytest -q tests/test_rust_policy_scan.py tests/test_policy_scan_contract.py
export CARGO_TARGET_DIR="$(mktemp -d)"
cargo test --manifest-path rust/Cargo.toml --locked
cargo clippy --manifest-path rust/Cargo.toml --all-targets --locked -- -D warnings
python scripts/benchmark_policy_backends.py --files 20000 --repeats 7
python scripts/benchmark_policy_backends.py --workspace /path/to/project --read-path /path/to/conda
```

Use an unprivileged Linux account to test permission-denied behavior. The default
Python test suite skips native differential cases when the companion is absent;
CI must install and import the extension before running them. Filesystems that
reject undecodable filenames skip that filesystem case; in-memory Rust tests
still cover byte-preserving matching and ordering. Embedded Python Rust tests
may need the interpreter's shared-library directory in `LD_LIBRARY_PATH` (Linux)
or `DYLD_FALLBACK_LIBRARY_PATH` (macOS/Conda).

Binding reference: [PyO3 parallelism and GIL release](https://pyo3.rs/v0.27.2/parallelism).
