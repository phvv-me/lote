"""The sync preflight: every editable path-dep a chefe manifest installs must be in the allowlist.

This guards the failure that silently broke a host once: ``chefe.toml`` gained a ``packages/atpx``
editable dependency, but ``lote.toml``'s ``[sync].include`` was not updated, so the host got no
``packages/atpx`` source and the remote ``chefe install`` died on a bare ``No such file``.
"""

from lote.models.config import editable_path_deps, uncovered_path_deps

MANIFEST = """
[python.deps]
numpy = ">=1.0"
lote = { path = "packages/lote", editable = true }
atpx = { path = "packages/atpx", editable = true }

[envs.serving.on.linux-64.python.deps]
mainboard = { path = "packages/mainboard", editable = true }
"""


def test_editable_path_deps_finds_nested(tmp_path):
    """Path-deps are collected from every ``deps`` table, including a nested env overlay."""
    manifest = tmp_path / "chefe.toml"
    manifest.write_text(MANIFEST)
    assert editable_path_deps(manifest) == {"packages/lote", "packages/atpx", "packages/mainboard"}


def test_version_strings_are_not_path_deps(tmp_path):
    """A plain version spec (no inline table) is not a path dep."""
    manifest = tmp_path / "chefe.toml"
    manifest.write_text('[python.deps]\nnumpy = ">=1.0"\n')
    assert editable_path_deps(manifest) == set()


def test_missing_manifest_yields_nothing(tmp_path):
    """A repo without a chefe manifest is simply unaffected."""
    assert editable_path_deps(tmp_path / "absent.toml") == set()


def test_absolute_path_is_not_a_repo_relative_dep(tmp_path):
    """An absolute `path` is a system tool, not a repo-relative editable dep, so it is skipped."""
    manifest = tmp_path / "chefe.toml"
    manifest.write_text('[python.deps]\ntool = { path = "/opt/tool", editable = true }\n')
    assert editable_path_deps(manifest) == set()


def test_uncovered_flags_a_dep_the_allowlist_omits(tmp_path):
    """The exact regression: an include that ships lote and mainboard but not atpx flags atpx."""
    manifest = tmp_path / "chefe.toml"
    manifest.write_text(MANIFEST)
    shipped = ["packages/lote", "packages/mainboard"]
    assert uncovered_path_deps(manifest, shipped) == ["packages/atpx"]


def test_a_parent_include_covers_its_children(tmp_path):
    """Including the parent directory ships every dep under it, so nothing is uncovered."""
    manifest = tmp_path / "chefe.toml"
    manifest.write_text(MANIFEST)
    assert uncovered_path_deps(manifest, ["packages"]) == []
