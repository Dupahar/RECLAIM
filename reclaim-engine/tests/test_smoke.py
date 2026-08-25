"""Phase 0 smoke test — proves the toolchain and package import work."""

import reclaim


def test_package_imports_and_has_version():
    assert reclaim.__version__ == "0.0.1"
