"""A cargo target that holds a superseded release: the version it carries decides whether it may go.

The release-runtime-rollback class kept every cargo target that holds a signed release artifact, because the installed
version and the single rollback must survive. The class never asked WHICH release it held, so a target with a build the
host replaced long ago was held for good. These tests are the rule: the version the artifact carries is read (a
bundle's own CFBundleShortVersionString, a disk image or package's version in its file name), and the target may go
only when every artifact it holds carries a version older than the installed one, and none of them is the installed
version or the retained rollback. Everything that cannot be read is kept.
"""
from __future__ import annotations

import os
import plistlib
from pathlib import Path
from unittest import TestCase, mock

from trimreaper import core

from helpers import Home

KEPT = "retention-preserved:release-runtime-rollback"


class ReleaseProductNameBoundary(TestCase):
    def test_product_name_boundaries(self):
        cases = (
            ("UltraTerm-Helper_1.0.0.pkg", "UltraTerm", False),
            ("UltraTerm_Helper_1.0.0.pkg", "UltraTerm", False),
            ("UltraTerm Helper_1.0.0.pkg", "UltraTerm", False),
            ("UltraTerm.Helper_1.0.0.pkg", "UltraTerm", False),
            ("UltraTerm+Helper_1.0.0.pkg", "UltraTerm", False),
            ("uLtRaTeRm-hElPeR_1.0.0.pkg", "ULTRATERM", False),
            ("UltraTerm-  _Helper_1.0.0.pkg", "UltraTerm", False),
            ("UltraTermHelper_1.0.0.pkg", "UltraTerm", False),
            ("MyUltraTerm_1.0.0.pkg", "UltraTerm", False),
            ("ultra-term-helper_1.0.0.pkg", "UltraTerm", False),
            ("my-app-helper_1.0.0.pkg", "My App", False),
            ("UltraTerm.app-Helper_1.0.0.pkg", "UltraTerm", False),
            ("UltraTerm.pkghelper_1.0.0.pkg", "UltraTerm", False),
            ("UltraTerm-vHelper_1.0.0.pkg", "UltraTerm", False),
            ("UltraTerm-v1Helper_1.0.0.pkg", "UltraTerm", False),
            ("UltraTerm-2Pro_1.0.0.pkg", "UltraTerm", False),
            ("UltraTerm (Helper) 1.0.0.pkg", "UltraTerm", False),
            ("UltraTerm–Helper_1.0.0.pkg", "UltraTerm", False),
            ("UltraTermv3.7.0.pkg", "UltraTerm", False),
            ("ultra-term-2Pro_1.0.0.pkg", "UltraTerm", False),
            ("Acme-UltraTerm-1.0.0.pkg", "UltraTerm", False),
            ("UltraTerm-3.8.4.pkg", "UltraTerm", True),
            ("UltraTerm_3.8.4_aarch64.dmg", "UltraTerm", True),
            ("UltraTerm.3.8.4.tgz", "UltraTerm", True),
            ("UltraTerm3.8.4.zip", "UltraTerm", True),
            ("UltraTerm-v3.8.4.pkg", "UltraTerm", True),
            ("uLtRaTeRm-3.8.4.PKG", "ULTRATERM", True),
            ("ultra-term-3.8.4.tar.xz", "UltraTerm", True),
            ("my-app-3.8.4.tar.xz", "My App", True),
            ("UltraTerm.app.tgz", "UltraTerm", True),
            ("UltraTerm.app.zip.sig", "UltraTerm", True),
            ("UltraTerm.app-3.8.4.tgz", "UltraTerm", True),
            ("UltraTerm", "UltraTerm", True),
            ("UltraTerm-3.8.4.pkg", "", False),
        )
        for name, product, expected in cases:
            with self.subTest(name=name, product=product):
                self.assertEqual(core.reap_release_mentions_product(name, product), expected)
        for suffix in core.REAP_RELEASE_SHAPES + core.REAP_RELEASE_SIGNATURES:
            with self.subTest(suffix=suffix):
                self.assertTrue(core.reap_release_mentions_product("UltraTerm" + suffix, "UltraTerm"))
        for name in ("UltraTerm-Helper_1.0.0.pkg", "UltraTerm-osx-3.8.4.nupkg"):
            with self.subTest(candidate=name):
                self.assertFalse(core.reap_release_mentions_product(name, "UltraTerm"))
                self.assertTrue(core.reap_release_mentions_product(name, "UltraTerm", identity=False))


class SupersededReleaseTarget(Home):
    """Real clones, real bundles, a real plist: the rule is proved on the same shapes the host holds."""

    def setUp(self):
        super().setUp()
        self.installed = self.bundle("3.8.4")
        self.rollback = self.bundle("3.8.3")
        self.superseded = self.bundle("3.7.0")

    IDENTIFIER = "com.example.ultraterm"

    def bundle(self, version: str, identifier: str | None = None, name: str = "UltraTerm.app") -> Path:
        """A macOS app bundle that carries its version and identifier where the installer reads them."""
        path = self.tmp / ("app-" + version) / name
        (path / "Contents").mkdir(parents=True)
        (path / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
            {"CFBundleShortVersionString": version, "CFBundleIdentifier": identifier or self.IDENTIFIER}))
        return path

    def configure(self, installed: Path | None = None, rollback: Path | None = None):
        self.write_config({"release": {"installedApp": str(installed) if installed else None,
                                       "rollbackApp": str(rollback) if rollback else None}})

    def target_with(self, repo: Path, version: str | None = "3.7.0", name: str = "UltraTerm.app",
                    layout: str = "release/bundle/macos", identifier: str | None = None) -> Path:
        """A cargo target whose release bundle carries `version` (None: a bundle with no readable version)."""
        target = repo / "target" / layout / name
        (target / "Contents" / "MacOS").mkdir(parents=True, exist_ok=True)
        (target / "Contents" / "MacOS" / "UltraTerm").write_bytes(b"binary")
        if version is not None:
            (target / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
                {"CFBundleShortVersionString": version, "CFBundleIdentifier": identifier or self.IDENTIFIER}))
        return repo / "target"

    def assertKept(self, out, reason: str = KEPT):
        self.assertEqual(out["deleted"], 0, out)
        self.assertEqual(out["items"][0]["reason"], reason, out["items"])

    # ---- the rule -------------------------------------------------------------------------------

    def test_a_superseded_release_target_is_reclaimed(self):
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0001")
        target = self.target_with(repo, "3.7.0")
        self.add_job("J-0001", repo, finished_ago=3600)

        out = self.cli("reap", "--job", "J-0001")

        self.assertFalse(target.exists(), "a target holding only a superseded release is reclaimable")
        self.assertEqual(out["deleted"], 1, out)

    def test_the_installed_version_the_rollback_and_a_newer_build_are_kept(self):
        self.configure(self.installed, self.rollback)
        for index, version in enumerate(("3.8.4", "3.8.3", "3.9.0"), start=2):
            repo = self.clone(f"app-j000{index}")
            target = self.target_with(repo, version)
            self.add_job(f"J-000{index}", repo, finished_ago=3600)
            out = self.cli("reap", "--job", f"J-000{index}")
            self.assertTrue(target.exists(), f"{version} must survive")
            self.assertKept(out)

    def test_an_unreadable_version_keeps_the_target(self):
        # The bundle names the product but carries no version: the version is what cannot be read, so the folder is kept.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0005")
        target = self.target_with(repo, None)
        (repo / "target/release/bundle/macos/UltraTerm.app/Contents/Info.plist").write_bytes(
            plistlib.dumps({"CFBundleIdentifier": self.IDENTIFIER}))
        self.add_job("J-0005", repo, finished_ago=3600)

        out = self.cli("reap", "--job", "J-0005")

        self.assertTrue(target.exists())
        self.assertKept(out)

    def test_an_unknown_installed_or_rollback_version_keeps_the_target(self):
        repo = self.clone("app-j0006")
        target = self.target_with(repo, "3.7.0")
        self.add_job("J-0006", repo, finished_ago=3600)
        for installed, rollback in ((None, self.rollback), (self.installed, None), (None, None)):
            with self.subTest(installed=installed, rollback=rollback):
                self.configure(installed, rollback)
                out = self.cli("reap", "--job", "J-0006")
                self.assertTrue(target.exists())
                self.assertKept(out)

    def test_a_target_without_any_release_artifact_keeps_the_class(self):
        # The class exists for the release a target holds. A target with a release directory and no artifact at all is
        # not proof of a release, but it is not proof of its absence either: over-retention is the safe answer.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0007")
        target = self.put(repo, "target/release/ultraterm", b"binary").parent.parent
        self.add_job("J-0007", repo, finished_ago=3600)

        out = self.cli("reap", "--job", "J-0007")

        self.assertTrue(target.exists())
        self.assertKept(out)

    def test_a_disk_image_carries_its_version_in_its_name(self):
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0008")
        self.put(repo, "target/release/bundle/dmg/UltraTerm_3.7.0_aarch64.dmg", b"image")
        self.put(repo, "target/release/bundle/dmg/UltraTerm_3.8.4_aarch64.dmg", b"image")
        self.add_job("J-0008", repo, finished_ago=3600)

        out = self.cli("reap", "--job", "J-0008")

        self.assertTrue((repo / "target").exists())
        self.assertKept(out)

    def test_a_disk_image_of_a_superseded_version_is_reclaimed(self):
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0009")
        self.put(repo, "target/release/bundle/dmg/UltraTerm_3.7.0_aarch64.dmg", b"image")
        self.add_job("J-0009", repo, finished_ago=3600)

        out = self.cli("reap", "--job", "J-0009")

        self.assertFalse((repo / "target").exists())
        self.assertEqual(out["deleted"], 1, out)

    def test_another_products_release_keeps_the_target(self):
        # The version of another product says nothing about the installed one: a bundle with another bundle identifier,
        # and a file name that is not the installed product's name, are both kept.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0039")
        target = self.target_with(repo, "3.7.0")
        other = repo / "target" / "release" / "bundle" / "macos" / "Other.app"
        (other / "Contents").mkdir(parents=True)
        (other / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
            {"CFBundleShortVersionString": "1.0.0", "CFBundleIdentifier": "com.example.other"}))
        self.add_job("J-0039", repo, finished_ago=3600)
        out = self.cli("reap", "--job", "J-0039")
        self.assertTrue(target.exists())
        self.assertKept(out)

    def test_another_products_file_name_keeps_the_target(self):
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0040")
        self.put(repo, "target/release/bundle/dmg/Other_1.0.0_aarch64.dmg", b"image")
        self.add_job("J-0040", repo, finished_ago=3600)
        out = self.cli("reap", "--job", "J-0040")
        self.assertTrue((repo / "target").exists())
        self.assertKept(out)

    def test_a_longer_word_that_contains_the_product_name_keeps_the_target(self):
        # "MyUltraTerm_1.0.0.zip" is not the product's file: the product's name is in the middle of a longer word.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0111")
        self.target_with(repo, "3.7.0")
        self.put(repo, "target/release/bundle/MyUltraTerm_1.0.0.zip", b"image")
        self.add_job("J-0111", repo, finished_ago=3600)
        out = self.cli("reap", "--job", "J-0111")
        self.assertTrue((repo / "target").exists())
        self.assertKept(out)

    def test_a_file_name_that_only_starts_with_the_product_name_keeps_the_target(self):
        # "UltraTermHelper_1.0.0.pkg" is not the installed product's artifact: the name is the product's name followed by
        # something else, and its version says nothing about the installed one.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0041")
        self.put(repo, "target/release/bundle/pkg/UltraTermHelper_1.0.0.pkg", b"package")
        self.add_job("J-0041", repo, finished_ago=3600)
        out = self.cli("reap", "--job", "J-0041")
        self.assertTrue((repo / "target").exists())
        self.assertKept(out)

    def test_a_foreign_helper_installer_keeps_a_superseded_product_bundle(self):
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0300")
        target = self.target_with(repo, "3.7.0")
        installer = self.put(repo, "target/release/bundle/pkg/UltraTerm-Helper_1.0.0.pkg", b"package")
        self.add_job("J-0300", repo, finished_ago=3600)

        artifacts, complete = core.reap_target_release_artifacts(str(target), "UltraTerm")
        self.assertTrue(complete)
        helper = next(a for a in artifacts if a["path"].endswith(installer.name))
        self.assertFalse(helper["product"], helper)
        self.assertIsNone(helper["version"], helper)
        self.assertFalse(core.reap_target_release_superseded(str(target)))

        out = self.cli("reap", "--job", "J-0300")

        self.assertTrue(installer.exists())
        self.assertTrue((target / "release/bundle/macos/UltraTerm.app").exists())
        self.assertKept(out)

    def test_ambiguous_product_formats_keep_the_whole_target(self):
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0305")
        target = self.target_with(repo, "3.7.0")
        self.add_job("J-0305", repo, finished_ago=3600)
        for name in ("UltraTerm-Helper_1.0.0.tar.xz", "UltraTerm-Helper_1.0.0.nupkg",
                     "UltraTerm-2Pro_1.0.0.pkg", "UltraTerm (Helper) 1.0.0.pkg",
                     "UltraTerm–Helper_1.0.0.pkg", "UltraTermv3.7.0.pkg",
                     "Acme-UltraTerm-1.0.0.pkg"):
            with self.subTest(name=name):
                installer = self.put(repo, "target/" + name, b"package")
                artifacts, complete = core.reap_target_release_artifacts(str(target), "UltraTerm")
                self.assertTrue(complete)
                artifact = next(a for a in artifacts if a["path"] == name)
                self.assertFalse(artifact["product"], artifact)
                self.assertIsNone(artifact["version"], artifact)
                self.assertFalse(core.reap_target_release_superseded(str(target)))
                self.assertKept(self.cli("reap", "--job", "J-0305"))
                self.assertTrue(installer.exists())
                installer.unlink()  # isolate each synthetic artifact from the next subtest

    def test_a_product_package_still_uses_its_release_version(self):
        self.configure(self.installed, self.rollback)
        for index, (version, reclaim) in enumerate((("3.7.0", True), ("3.8.4", False),
                                                   ("3.8.3", False), ("3.9.0", False)), start=301):
            with self.subTest(version=version):
                repo = self.clone(f"app-j{index:04d}")
                target = self.target_with(repo, "3.7.0")
                self.put(repo, f"target/release/bundle/pkg/UltraTerm-{version}.pkg", b"package")
                self.add_job(f"J-{index:04d}", repo, finished_ago=3600)
                self.assertEqual(core.reap_target_release_superseded(str(target)), reclaim)
                out = self.cli("reap", "--job", f"J-{index:04d}")
                self.assertEqual(target.exists(), not reclaim)
                if reclaim:
                    self.assertEqual(out["deleted"], 1, out)
                else:
                    self.assertKept(out)

    def test_a_bundle_without_an_identifier_keeps_the_target(self):
        # The product's own bundle, superseded, but without the identifier: a bundle that does not say which product it
        # is cannot be compared with the installed one. The name alone would have released it.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0042")
        target = self.target_with(repo, "3.7.0")
        (repo / "target/release/bundle/macos/UltraTerm.app/Contents/Info.plist").write_bytes(
            plistlib.dumps({"CFBundleShortVersionString": "3.7.0"}))
        self.add_job("J-0042", repo, finished_ago=3600)
        out = self.cli("reap", "--job", "J-0042")
        self.assertTrue(target.exists())
        self.assertKept(out)

    def test_an_installed_app_without_an_identifier_keeps_every_target(self):
        # Without the product's identity the rule cannot compare anything: every target is kept. The artifact is a file
        # whose name carries the version, so nothing but the missing identifier keeps the folder.
        repo = self.clone("app-j0043")
        self.put(repo, "target/release/bundle/dmg/UltraTerm_3.7.0_aarch64.dmg", b"image")
        target = repo / "target"
        self.add_job("J-0043", repo, finished_ago=3600)
        bare = self.tmp / "bare" / "UltraTerm.app"
        (bare / "Contents").mkdir(parents=True)
        (bare / "Contents" / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "3.8.4"}))
        self.configure(bare, self.rollback)
        out = self.cli("reap", "--job", "J-0043")
        self.assertTrue(target.exists())
        self.assertKept(out)

    def test_an_installed_app_with_another_product_name_keeps_every_target(self):
        # The product's name is part of the comparison: an artifact named for another product says nothing about it.
        repo = self.clone("app-j0045")
        self.put(repo, "target/release/bundle/dmg/UltraTerm_3.7.0_aarch64.dmg", b"image")
        target = repo / "target"
        self.add_job("J-0045", repo, finished_ago=3600)
        other = self.tmp / "other" / "Other.app"
        (other / "Contents").mkdir(parents=True)
        (other / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
            {"CFBundleShortVersionString": "3.8.4", "CFBundleIdentifier": self.IDENTIFIER}))
        self.configure(other, self.rollback)
        out = self.cli("reap", "--job", "J-0045")
        self.assertTrue(target.exists())
        self.assertKept(out)

    def test_a_directory_named_like_an_artifact_keeps_the_target(self):
        # A directory is not an artifact: nothing can be read from it, so the folder is kept.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0044")
        self.target_with(repo, "3.7.0")
        (repo / "target" / "release" / "bundle" / "dmg" / "UltraTerm_3.7.0_aarch64.dmg").mkdir(parents=True)
        self.add_job("J-0044", repo, finished_ago=3600)
        out = self.cli("reap", "--job", "J-0044")
        self.assertTrue((repo / "target").exists())
        self.assertKept(out)

    def test_an_archive_of_the_installed_version_keeps_the_target(self):
        # An archive or an installer of the product is a release artifact too, whatever its format: the installed
        # version in one of them keeps the whole target.
        self.configure(self.installed, self.rollback)
        for index, name in enumerate(("UltraTerm_3.8.4_aarch64.zip", "ultraterm-3.8.4.tgz", "UltraTerm_3.8.4_aarch64.tar.gz",
                                      "UltraTerm_3.8.4_aarch64.msi", "UltraTerm_3.8.4_aarch64.exe", "UltraTerm_3.8.4_amd64.deb",
                                      "UltraTerm_3.8.4_x86_64.rpm", "UltraTerm_3.8.4_amd64.appimage"), start=46):
            with self.subTest(name=name):
                repo = self.clone(f"app-j00{index}")
                self.target_with(repo, "3.7.0")
                self.put(repo, f"target/release/bundle/{name}", b"image")
                self.add_job(f"J-00{index}", repo, finished_ago=3600)
                out = self.cli("reap", "--job", f"J-00{index}")
                self.assertTrue((repo / "target").exists(), name)
                self.assertKept(out)

    def test_an_archive_of_a_superseded_version_is_reclaimed(self):
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0054")
        self.put(repo, "target/release/bundle/UltraTerm_3.7.0_aarch64.zip", b"image")
        self.add_job("J-0054", repo, finished_ago=3600)
        out = self.cli("reap", "--job", "J-0054")
        self.assertFalse((repo / "target").exists())
        self.assertEqual(out["deleted"], 1, out)

    def test_a_product_archive_whose_name_is_not_the_plain_version_keeps_the_target(self):
        # A release is published under many names: "UltraTerm.app.tgz", "UltraTerm.3.8.4.tgz", "UltraTerm_3.8.4+5.tgz"
        # and "UltraTerm_3.8.4_2026.10.21.tgz" all name the product and carry the installed version. The version of the
        # last one is not clear (it carries two), which keeps the folder as well.
        self.configure(self.installed, self.rollback)
        for index, name in enumerate(("UltraTerm.app.tgz", "UltraTerm.3.8.4.tgz", "UltraTerm_3.8.4+5.tgz",
                                      "UltraTerm_3.8.4_2026.10.21.tgz", "UltraTerm-3.8.4.tgz"), start=60):
            with self.subTest(name=name):
                repo = self.clone(f"app-j00{index}")
                self.target_with(repo, "3.7.0")
                self.put(repo, f"target/release/bundle/{name}", b"image")
                self.add_job(f"J-00{index}", repo, finished_ago=3600)
                out = self.cli("reap", "--job", f"J-00{index}")
                self.assertTrue((repo / "target").exists(), name)
                self.assertKept(out)

    def test_an_archive_of_another_product_keeps_the_target(self):
        # An archive of something else is a release artifact of the class, and its version says nothing about the
        # installed one: the folder is kept.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0065")
        self.target_with(repo, "3.7.0")
        self.put(repo, "target/release/bundle/Other_1.0.0.zip", b"image")
        self.add_job("J-0065", repo, finished_ago=3600)
        out = self.cli("reap", "--job", "J-0065")
        self.assertTrue((repo / "target").exists())
        self.assertKept(out)

    def test_an_archive_that_is_not_the_product_is_ignored(self):
        # A runtime archive is not a release: it does not name the product, so it does not keep the folder.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0066")
        self.target_with(repo, "3.7.0")
        self.put(repo, "target/release/pi-runtime/steak-pi.tgz", b"archive")
        self.add_job("J-0066", repo, finished_ago=3600)
        out = self.cli("reap", "--job", "J-0066")
        self.assertFalse((repo / "target").exists())
        self.assertEqual(out["deleted"], 1, out)

    def test_a_product_whose_name_carries_a_separator_keeps_the_target(self):
        # "My App" is named by "my-app-1.2.3.tar.xz" too: the separators are left out on both sides.
        spaced = self.bundle("3.8.4", name="My App.app")
        self.configure(spaced, self.rollback)
        repo = self.clone("app-j0101")
        target = self.target_with(repo, "3.7.0", name="My App.app")
        self.put(repo, "target/release/bundle/my-app-3.8.4.tar.xz", b"image")
        self.add_job("J-0101", repo, finished_ago=3600)
        out = self.cli("reap", "--job", "J-0101")
        self.assertTrue(target.exists())
        self.assertKept(out)

    def test_a_product_release_whose_name_does_not_start_with_the_product_keeps_the_target(self):
        # "UltraTerm3.8.4.tgz" has no separator after the product's name, and "ultra-term-3.8.4.tar.xz" spells it
        # differently: both name the product and both carry the installed version, so the folder is kept.
        self.configure(self.installed, self.rollback)
        for index, name in enumerate(("UltraTerm3.8.4.tgz", "ultra-term-3.8.4.tar.xz", "UltraTerm3.8.4.zip"), start=100):
            with self.subTest(name=name):
                repo = self.clone(f"app-j0{index:02d}")
                self.target_with(repo, "3.7.0")
                self.put(repo, f"target/release/bundle/{name}", b"image")
                self.add_job(f"J-0{index:02d}", repo, finished_ago=3600)
                out = self.cli("reap", "--job", f"J-0{index:02d}")
                self.assertTrue((repo / "target").exists(), name)
                self.assertKept(out)

    def test_a_product_archive_in_any_published_format_keeps_the_target(self):
        # A product archive in a format the scan does not name would be missed; every format a release is published in
        # is named, so the folder is kept.
        self.configure(self.installed, self.rollback)
        for index, name in enumerate(("UltraTerm.tar", "UltraTerm.iso", "UltraTerm.img", "UltraTerm.snap",
                                      "UltraTerm.flatpak", "UltraTerm.rar", "UltraTerm.7z", "UltraTerm.txz",
                                      "UltraTerm.zst", "UltraTerm.lz", "UltraTerm.tgz", "UltraTerm.gz",
                                      "UltraTerm.bz2", "UltraTerm.xz", "UltraTerm.lzma", "UltraTerm.lzh",
                                      "UltraTerm.cab", "UltraTerm.msu"), start=70):
            with self.subTest(name=name):
                repo = self.clone(f"app-j00{index}")
                self.target_with(repo, "3.7.0")
                self.put(repo, f"target/release/bundle/{name}", b"image")
                self.add_job(f"J-00{index}", repo, finished_ago=3600)
                out = self.cli("reap", "--job", f"J-00{index}")
                self.assertTrue((repo / "target").exists(), name)
                self.assertKept(out)

    def test_an_installer_of_another_product_keeps_the_target(self):
        # An installer of something else is a release artifact of the class and its version says nothing about the
        # installed one: the folder is kept. Each suffix is named on its own here.
        self.configure(self.installed, self.rollback)
        for index, name in enumerate(("Other_1.0.0.zip", "Other_1.0.0.tar.gz", "Other_1.0.0.msi", "Other_1.0.0.exe",
                                      "Other_1.0.0.appimage", "Other_1.0.0.deb", "Other_1.0.0.rpm",
                                      "Setup.msixbundle", "Install.mpkg", "Setup.appx", "Setup.appxbundle",
                                      "Setup.xip"), start=80):
            with self.subTest(name=name):
                repo = self.clone(f"app-j00{index}")
                self.target_with(repo, "3.7.0")
                self.put(repo, f"target/release/bundle/{name}", b"image")
                self.add_job(f"J-00{index}", repo, finished_ago=3600)
                out = self.cli("reap", "--job", f"J-00{index}")
                self.assertTrue((repo / "target").exists(), name)
                self.assertKept(out)

    def test_a_bundle_whose_name_and_plist_disagree_keeps_the_target(self):
        # The name says the installed version, the bundle's own plist says an older one: which one is the release is
        # not said, so the folder is kept.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0092")
        target = self.target_with(repo, "3.7.0")
        bundle = repo / "target" / "release" / "bundle" / "macos" / "UltraTerm_3.8.4.app"
        (bundle / "Contents").mkdir(parents=True)
        (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
            {"CFBundleShortVersionString": "3.7.0", "CFBundleIdentifier": self.IDENTIFIER}))
        self.add_job("J-0092", repo, finished_ago=3600)
        out = self.cli("reap", "--job", "J-0092")
        self.assertTrue(target.exists())
        self.assertKept(out)

    def test_malformed_bundle_versions_keep_the_target(self):
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0200")
        target = self.target_with(repo)
        plist = target / "release/bundle/macos/UltraTerm.app/Contents/Info.plist"
        for version in ("3.7.0garbage", "3.7.0.1", "3.7.0\n", "v3.7.0", "3.7.0-beta", "9" * 5000 + ".0.0"):
            with self.subTest(version=version[:40]):
                plist.write_bytes(plistlib.dumps({"CFBundleShortVersionString": version,
                                                  "CFBundleIdentifier": self.IDENTIFIER}))
                self.assertFalse(core.reap_target_release_superseded(str(target)))

    def test_malformed_file_versions_keep_the_target(self):
        self.configure(self.installed, self.rollback)
        for index, name in enumerate(("UltraTerm_3.7.0garbage.dmg", "UltraTerm_3.7.0.1.dmg",
                                      "UltraTerm_3.7.0_3.7.0.dmg"), start=210):
            with self.subTest(name=name):
                repo = self.clone(f"app-j{index:04d}")
                self.put(repo, f"target/release/bundle/{name}", b"image")
                self.assertFalse(core.reap_target_release_superseded(str(repo / "target")))

    def test_malformed_host_versions_keep_the_target(self):
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0201")
        target = self.target_with(repo)
        marks = core.reap_release_marks()
        for field in ("installed", "rollback"):
            for version in ("3.8.4garbage", "3.8.4.1", "3.8.4\n", "v3.8.4", "3.8.4-beta"):
                with self.subTest(field=field, version=version):
                    with mock.patch.object(core, "reap_release_marks", return_value={**marks, field: version}):
                        self.assertFalse(core.reap_target_release_superseded(str(target)))

    def test_ambiguous_or_misleading_bundle_names_keep_the_target(self):
        self.configure(self.installed, self.rollback)
        for index, name in enumerate(("UltraTerm_3.8.4_3.7.0.app", "UltraTerm-osx-3.8.4.app",
                                      "ultra-term-3.8.4.app", "UltraTerm_3.7.0_1.2.3.app"), start=202):
            with self.subTest(name=name):
                repo = self.clone(f"app-j{index:04d}")
                target = self.target_with(repo, "3.7.0", name=name)
                self.assertFalse(core.reap_target_release_superseded(str(target)))

    def test_a_product_named_file_in_an_unknown_format_keeps_the_target(self):
        # "UltraTerm-3.8.4-osx-full.nupkg" names the product and carries the installed version, but it is not a format
        # the rule knows: what it holds is not a release the rule may compare, so the folder is kept.
        self.configure(self.installed, self.rollback)
        for index, name in enumerate(("UltraTerm-3.8.4-osx-full.nupkg", "ultraterm-3.8.4-aarch64-apple-darwin"), start=110):
            with self.subTest(name=name):
                repo = self.clone(f"app-j0{index}")
                self.target_with(repo, "3.7.0")
                self.put(repo, f"target/release/bundle/{name}", b"package")
                self.add_job(f"J-0{index}", repo, finished_ago=3600)
                out = self.cli("reap", "--job", f"J-0{index}")
                self.assertTrue((repo / "target").exists(), name)
                self.assertKept(out)

    def test_a_product_file_with_a_version_in_any_name_keeps_the_target(self):
        # The product's name and a version, in a format the rule does not know and in a name the pattern does not
        # read: the version is unclear, so the folder is kept.
        self.configure(self.installed, self.rollback)
        for index, name in enumerate(("UltraTerm_3.8.4_2026.10.21.nupkg", "UltraTerm-osx-3.8.4.nupkg",
                                      "x86_64-UltraTerm-3.8.4.nupkg", "UltraTerm3.8.4.nupkg",
                                      "ultra-term-3.8.4.nupkg", "UltraTerm@3.8.4.tgz"), start=120):
            with self.subTest(name=name):
                repo = self.clone(f"app-j0{index}")
                self.target_with(repo, "3.7.0")
                self.put(repo, f"target/release/bundle/{name}", b"package")
                self.add_job(f"J-0{index}", repo, finished_ago=3600)
                out = self.cli("reap", "--job", f"J-0{index}")
                self.assertTrue((repo / "target").exists(), name)
                self.assertKept(out)

    def test_a_crate_archive_does_not_keep_a_superseded_target(self):
        # A crate archive is the library's version, not the app's: it is not recorded, so it does not keep a target
        # whose only release is superseded.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0126")
        self.target_with(repo, "3.7.0")
        self.put(repo, "target/package/ultraterm-0.1.0.crate", b"crate")
        self.add_job("J-0126", repo, finished_ago=3600)
        out = self.cli("reap", "--job", "J-0126")
        self.assertFalse((repo / "target").exists())
        self.assertEqual(out["deleted"], 1, out)

    def test_a_bundle_owned_by_another_user_is_not_read(self):
        # A bundle that is not this user's is not read: its version stays unknown, so the folder is kept.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0127")
        target = self.target_with(repo, "3.7.0")
        bundle = repo / "target" / "release" / "bundle" / "macos" / "UltraTerm.app"
        with mock.patch.object(core.os, "getuid", return_value=os.getuid() + 1):
            fd = os.open(bundle, os.O_RDONLY | os.O_DIRECTORY)
            try:
                self.assertEqual(core.reap_release_bundle_info(fd), (None, None))
            finally:
                os.close(fd)
            self.assertFalse(core.reap_target_release_superseded(str(target)))

    def test_a_crate_archive_is_not_a_release_of_the_app(self):
        # "ultraterm-0.1.0.crate" is the library's version, not the app's: it is not a release artifact, so a target
        # that holds it and nothing else is not released.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0103")
        self.put(repo, "target/release/ultraterm", b"binary")
        self.put(repo, "target/package/ultraterm-0.1.0.crate", b"crate")
        self.add_job("J-0103", repo, finished_ago=3600)
        out = self.cli("reap", "--job", "J-0103")
        self.assertTrue((repo / "target").exists())
        self.assertKept(out)

    def test_a_name_with_two_different_versions_keeps_the_target(self):
        # Which of the two versions is the release? The name does not say, so the version is unreadable.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0010")
        self.put(repo, "target/release/bundle/dmg/UltraTerm_3.7.0_1.2.3_aarch64.dmg", b"image")
        self.add_job("J-0010", repo, finished_ago=3600)

        out = self.cli("reap", "--job", "J-0010")

        self.assertTrue((repo / "target").exists())
        self.assertKept(out)

    def test_a_signature_without_a_version_keeps_the_target(self):
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0011")
        self.put(repo, "target/release/bundle/macos/UltraTerm.app/Contents/MacOS/UltraTerm", b"binary")
        (repo / "target/release/bundle/macos/UltraTerm.app/Contents/Info.plist").write_bytes(plistlib.dumps(
            {"CFBundleShortVersionString": "3.7.0", "CFBundleIdentifier": self.IDENTIFIER}))
        self.put(repo, "target/release/bundle/dmg/UltraTerm.sig", b"signature")
        self.add_job("J-0011", repo, finished_ago=3600)

        out = self.cli("reap", "--job", "J-0011")

        self.assertTrue((repo / "target").exists())
        self.assertKept(out)

    # ---- the scan must find every artifact ----------------------------------------------------------------------------

    def test_the_installed_version_in_another_profile_directory_keeps_the_target(self):
        # A cargo profile is any directory under the target ("dist", "release-lto", a triple's own profile, a bundler's
        # example directory, a build directory): the scan walks all of them, so an installed-version bundle in one of
        # them keeps the whole target.
        self.configure(self.installed, self.rollback)
        layouts = ("dist/bundle/macos", "release-lto/bundle/macos", "aarch64-apple-darwin/release/bundle/macos",
                   "aarch64-apple-darwin/release-lto/bundle/macos", "release/examples/bundle/osx", "dist/build",
                   "release/build/UltraTerm/out")
        for index, layout in enumerate(layouts, start=12):
            with self.subTest(layout=layout):
                repo = self.clone(f"app-j00{index}")
                self.target_with(repo, "3.7.0")
                bundle = repo / "target" / layout / "UltraTerm.app"
                (bundle / "Contents").mkdir(parents=True)
                (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
                    {"CFBundleShortVersionString": "3.8.4", "CFBundleIdentifier": self.IDENTIFIER}))
                self.add_job(f"J-00{index}", repo, finished_ago=3600)
                out = self.cli("reap", "--job", f"J-00{index}")
                self.assertTrue((repo / "target").exists(), layout)
                self.assertKept(out)

    def test_a_superseded_bundle_in_another_profile_directory_is_reclaimed(self):
        # The mirror of the case above: the same layout holds only a superseded bundle, so the target goes. The layouts
        # are inside the release layout the class recognizes, so the rule is what releases them.
        self.configure(self.installed, self.rollback)
        for index, layout in enumerate(("release/examples/bundle/osx", "release/build/UltraTerm/out"), start=22):
            with self.subTest(layout=layout):
                repo = self.clone(f"app-j00{index}")
                self.target_with(repo, "3.7.0", layout=layout)
                self.add_job(f"J-00{index}", repo, finished_ago=3600)
                out = self.cli("reap", "--job", f"J-00{index}")
                self.assertFalse((repo / "target").exists(), layout)
                self.assertEqual(out["deleted"], 1, out)

    def test_a_scan_that_cannot_read_a_directory_keeps_the_target(self):
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0025")
        target = self.target_with_root_bundle(repo)
        self.add_job("J-0025", repo, finished_ago=3600)
        # A directory the scan may not read makes the inventory incomplete: the folder is kept, even though the
        # artifact it did find is superseded. The artifact sits at the top of the target, so it IS found before the
        # failure: the completeness flag is what keeps the folder.
        listing = core.reap_release_listing
        calls = {"n": 0}

        def fail_after_the_first(directory, budget):
            calls["n"] += 1
            return None if calls["n"] > 1 else listing(directory, budget)

        with mock.patch.object(core, "reap_release_listing", side_effect=fail_after_the_first):
            calls["n"] = 0  # the rule's own scan starts fresh: the first listing is the real one
            self.assertFalse(core.reap_target_release_superseded(str(target)))
            out = self.cli("reap", "--job", "J-0025")
        self.assertTrue(target.exists())
        self.assertKept(out)

    def test_a_scan_that_runs_out_of_budget_keeps_the_target(self):
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0026")
        target = self.target_with_root_bundle(repo)
        self.add_job("J-0026", repo, finished_ago=3600)
        # The budget runs out after the artifact was found: the scan is incomplete, so the folder is kept. Without the
        # completeness check the same target would be released below, which is what this test compares.
        with mock.patch.object(core, "REAP_RELEASE_SCAN_ENTRIES", 2):
            artifacts, complete = core.reap_target_release_artifacts(str(target))
            self.assertTrue(artifacts, "the artifact was found before the budget ran out")
            self.assertFalse(complete)
            out = self.cli("reap", "--job", "J-0026")
        self.assertTrue(target.exists())
        self.assertKept(out)
        out = self.cli("reap", "--job", "J-0026")
        self.assertFalse(target.exists(), "the same target goes with the whole budget")

    def test_a_scan_that_runs_out_of_depth_keeps_the_target(self):
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0027")
        target = self.target_with_root_bundle(repo)
        self.add_job("J-0027", repo, finished_ago=3600)
        with mock.patch.object(core, "REAP_RELEASE_SCAN_DEPTH", 0):
            artifacts, complete = core.reap_target_release_artifacts(str(target))
            self.assertTrue(artifacts, "the artifact was found before the cap")
            self.assertFalse(complete)
            out = self.cli("reap", "--job", "J-0027")
        self.assertTrue(target.exists())
        self.assertKept(out)

    def target_with_root_bundle(self, repo: Path, version: str = "3.7.0") -> Path:
        """A target whose bundle sits at the top of it, next to a directory the scan has to enter."""
        bundle = repo / "target" / "UltraTerm.app"
        (bundle / "Contents" / "MacOS").mkdir(parents=True)
        (bundle / "Contents" / "Info.plist").write_bytes(plistlib.dumps(
            {"CFBundleShortVersionString": version, "CFBundleIdentifier": self.IDENTIFIER}))
        self.put(repo, "target/sub/file", b"x")
        return repo / "target"

    # ---- the version read ----------------------------------------------------------------------

    def test_a_symlink_named_like_a_release_keeps_the_target(self):
        # The link is the product's own bundle name and points at a superseded bundle: a rule that followed the link
        # would read 3.7.0 and release the target. The link is not followed, so the name decides, and the folder is kept.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0028")
        target = self.target_with(repo, "3.7.0")
        link = repo / "target" / "release" / "bundle" / "other" / "UltraTerm.app"
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(self.superseded)
        self.add_job("J-0028", repo, finished_ago=3600)
        out = self.cli("reap", "--job", "J-0028")
        self.assertTrue(target.exists())
        self.assertKept(out)

    def test_a_plist_that_cannot_be_read_keeps_the_target(self):
        self.configure(self.installed, self.rollback)
        for index, make in enumerate((self._symlinked_plist, self._oversized_plist, self._fifo_plist, self._hardlinked_plist),
                                     start=29):
            with self.subTest(plist=make.__name__):
                repo = self.clone(f"app-j00{index}")
                target = self.target_with(repo, "3.7.0")
                make(repo / "target/release/bundle/macos/UltraTerm.app/Contents/Info.plist")
                self.add_job(f"J-00{index}", repo, finished_ago=3600)
                out = self.cli("reap", "--job", f"J-00{index}")
                self.assertTrue(target.exists())
                self.assertKept(out)

    def _symlinked_plist(self, plist: Path):
        # A SUPERSEDED plist: a rule that followed the link would read 3.7.0 and release the target.
        plist.unlink()
        plist.symlink_to(self.superseded / "Contents" / "Info.plist")

    def _oversized_plist(self, plist: Path):
        # A valid plist of exactly one byte over the limit: only the size rule can refuse it.
        data = plistlib.dumps({"CFBundleShortVersionString": "3.7.0", "CFBundleIdentifier": self.IDENTIFIER})
        plist.write_bytes(data + b" " * (core.REAP_RELEASE_VERSION_BYTES + 1 - len(data)))

    def _fifo_plist(self, plist: Path):
        plist.unlink()
        os.mkfifo(plist)

    def _hardlinked_plist(self, plist: Path):
        other = plist.parent / "Info.plist.bak"
        os.link(plist, other)

    def test_a_fifo_plist_that_a_writer_feeds_is_still_refused(self):
        # A fifo that a writer feeds a valid plist into: only the regular-file rule refuses it, not an empty read.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0093")
        target = self.target_with(repo, "3.7.0")
        plist = repo / "target/release/bundle/macos/UltraTerm.app/Contents/Info.plist"
        plist.unlink()
        os.mkfifo(plist)
        data = plistlib.dumps({"CFBundleShortVersionString": "3.7.0", "CFBundleIdentifier": self.IDENTIFIER})

        # Both ends of the pipe are held open here and the plist is already in it, so every read of it sees the data:
        # only the regular-file rule refuses it.
        reader = os.open(plist, os.O_RDONLY | os.O_NONBLOCK)
        writer = os.open(plist, os.O_WRONLY)
        os.write(writer, data)
        # One scan: the bundle's plist is a fifo, so it carries no version however much a writer feeds it.
        artifacts, complete = core.reap_target_release_artifacts(str(target), "UltraTerm")
        self.assertTrue(complete)
        self.assertEqual([a["version"] for a in artifacts], [None], artifacts)
        self.add_job("J-0093", repo, finished_ago=3600)
        out = self.cli("reap", "--job", "J-0093")
        os.close(writer)
        os.close(reader)
        self.assertTrue(target.exists())
        self.assertKept(out)

    def test_a_bundle_that_is_not_a_directory_keeps_the_target(self):
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0033")
        target = self.target_with(repo, "3.7.0")
        bundle = repo / "target" / "release" / "bundle" / "other" / "UltraTerm.app"
        bundle.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(bundle)
        self.add_job("J-0033", repo, finished_ago=3600)
        out = self.cli("reap", "--job", "J-0033")
        self.assertTrue(target.exists())
        self.assertKept(out)

    # ---- the class and the rule together -------------------------------------------------------------------------------

    def test_the_enclosing_clone_scan_follows_the_same_rule(self):
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0034")
        self.target_with(repo, "3.7.0")
        core.reap_protected_scan(str(repo), allow_git=True, markers=True, nested_disposable=True)  # a superseded release does not refuse the clone
        other = self.clone("app-j0035")
        self.target_with(other, "3.8.4")
        with self.assertRaisesRegex(core.ReapRefused, "retention-preserved:release-runtime-rollback"):
            core.reap_protected_scan(str(other), allow_git=True, markers=True, nested_disposable=True)

    def test_the_rule_reads_the_installed_and_rollback_versions_of_the_host(self):
        # The host's own installer paths are the source of the two versions: a wrong mark would release the wrong target.
        self.configure(self.installed, self.rollback)
        marks = core.reap_release_marks()
        self.assertEqual((marks["installed"], marks["rollback"]), ("3.8.4", "3.8.3"))
        repo = self.clone("app-j0036")
        target = self.target_with(repo, "3.7.0")
        self.assertTrue(core.reap_target_release_superseded(str(target)))
        self.assertTrue(core.reap_target_release_bearing(str(target)))
        other = self.clone("app-j0037")
        installed_target = self.target_with(other, "3.8.4")
        self.assertFalse(core.reap_target_release_superseded(str(installed_target)))

    def test_the_release_class_is_names_only_and_this_rule_does_not_widen_it(self):
        # A property of the class that predates this rule and is unchanged by it: the class is the engine's names-only
        # check over the release layouts, so a target whose only artifact sits outside them is not in the class. The
        # rule reads the versions of the artifacts a target IN the class holds; it never widens the class. Widening it
        # would hold more targets than today and is not this rule's business.
        self.configure(self.installed, self.rollback)
        repo = self.clone("app-j0038")
        target = self.target_with(repo, "3.8.4", layout="dist/bundle/macos")
        self.assertFalse(core.reap_target_release_bearing(str(target)))
        self.assertFalse(core.reap_target_release_superseded(str(target)),
                         "the rule alone would not release it either: it holds the installed version")
        other = self.clone("app-j0102")
        in_class = self.target_with(other, "3.8.4")
        self.assertTrue(core.reap_target_release_bearing(str(in_class)))
