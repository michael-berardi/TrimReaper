"""Retained-class names: a record, a credential, a key or a certificate is never disposable evidence.

The rule is a name test, not a size or age test: a folder is kept whole when any name below it belongs to the
retained class, and a camelCase name, a name in any position and a key or certificate extension are the same class.
"""
from helpers import Home, DAY
from trimreaper import core


class RetainedNames(Home):
    config_extra = {"policy": {"evidenceExpiryDays": 1, "markedEvidenceExpiryDays": 5, "cloneArchive": "keep"}}

    def test_records_are_retained_in_any_position_and_any_number(self):
        for name in ("report.md", "final-report.md", "review-receipt.md", "handoffs", "handoff.md", "summary.txt",
                     "summaries.json", "outcome.json", "receipts/receipt.jsonl", "evidence-expired.jsonl",
                     "protected-archive"):
            with self.subTest(name=name):
                self.assertTrue(core.marked_evidence_keep(name), name)

    def test_camel_case_is_the_same_class_as_a_hyphenated_name(self):
        for name in ("SubscriptionKey_X.p8", "AppleCerts", "SigningCert.p12", "CustomerData.json"):
            with self.subTest(name=name):
                self.assertTrue(core.marked_evidence_keep(name), name)

    def test_retained_classes_are_kept_whole(self):
        for name in ("release-notes.txt", "runtime-config.json", "rollback-2", "Old Customer List.csv", "customerdata.json",
                     "credentials.json", "secrets.env", "entitlement.bin", "notarization.json", "keychain.dump",
                     "provisioning.plist", "authkey.txt", "password.txt", "certificate.cer", "token.json", "id_ed25519",
                     "known_hosts", "authorized_keys", "netrc", "npmrc", "pypirc", "htpasswd", "keystore", "kubeconfig",
                     "aws-credentials", "service-account.json", "adminsdk.json", "gcloud.json", "firebase.json", "oauth.json"):
            with self.subTest(name=name):
                self.assertTrue(core.marked_evidence_keep(name), name)

    def test_key_certificate_and_profile_extensions_are_retained(self):
        for name in ("build.app", "image.dmg", "installer.pkg", "payload.ipa", "bundle.zip", "archive.tgz", "build.xcarchive",
                     "release.sig", "release.asc", "key.p8", "key.p12", "key.pem", "key.pfx", "cert.cer", "cert.crt",
                     "private.key", "request.csr", "store.jks", "store.keystore", "bundle.p7b", "bundle.p7c", "cert.srl",
                     "profile.mobileprovision", "tunnel.ovpn", "key.ppk"):
            with self.subTest(name=name):
                self.assertTrue(core.marked_evidence_keep(name), name)

    def test_ordinary_output_names_are_not_retained(self):
        for name in ("frame.png", "screenshot.png", "notes.txt", "build.log", "index.html", "data.csv", "cache.bin",
                     "prerelease.txt", "reporter.png", "summarised.txt", "handover.txt"):
            with self.subTest(name=name):
                self.assertFalse(core.marked_evidence_keep(name), name)


class RetainedNamesBelowTheFolder(Home):
    config_extra = {"policy": {"evidenceExpiryDays": 1, "markedEvidenceExpiryDays": 5, "cloneArchive": "keep"}}

    def test_a_nested_retained_name_keeps_the_whole_folder(self):
        self.add_job("J-0060", finished_ago=2 * DAY)
        root = self.home / ".cache/trimreaper/jobs/J-0060"
        child = self.put(root, "build/final-report.md").parent
        self.assertFalse(core.reap_no_retained_names(str(child)))
        out = self.cli("reap", "--job", "J-0060")
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue((child / "final-report.md").exists())

    def test_a_folder_without_a_retained_name_is_disposable(self):
        self.add_job("J-0060", finished_ago=2 * DAY)
        root = self.home / ".cache/trimreaper/jobs/J-0060"
        child = self.put(root, "build/frame.png").parent
        self.assertTrue(core.reap_no_retained_names(str(child)))
        out = self.cli("reap", "--job", "J-0060")
        self.assertEqual(out["deleted"], 1, out)
        self.assertFalse(child.exists())

    def test_a_nested_credential_keeps_the_whole_folder(self):
        self.add_job("J-0060", finished_ago=2 * DAY)
        root = self.home / ".cache/trimreaper/jobs/J-0060"
        child = self.put(root, "build/signing.p8").parent
        self.assertFalse(core.reap_no_retained_names(str(child)))
        out = self.cli("reap", "--job", "J-0060")
        self.assertEqual(out["deleted"], 0, out)
        self.assertTrue((child / "signing.p8").exists())


class DurableRecord(Home):
    config_extra = {"policy": {"evidenceExpiryDays": 1, "markedEvidenceExpiryDays": 5, "cloneArchive": "keep"}}

    def test_an_empty_report_is_not_a_durable_record(self):
        self.add_job("J-0060", finished_ago=6 * DAY)
        root = self.home / ".cache/trimreaper/jobs/J-0060"
        root.mkdir(parents=True, exist_ok=True)
        (root / "report.md").write_bytes(b"")
        self.assertFalse(core.marked_evidence_job("J-0060"))

    def test_a_written_report_is_a_durable_record(self):
        self.add_job("J-0060", finished_ago=6 * DAY)
        root = self.home / ".cache/trimreaper/jobs/J-0060"
        root.mkdir(parents=True, exist_ok=True)
        (root / "report.md").write_bytes(b"the record\n")
        self.assertTrue(core.marked_evidence_job("J-0060"))
