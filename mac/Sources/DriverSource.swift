// 1401
// Copyright (c) 2026 NullMoth Systems. SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0

import Foundation

// A driver package from another source than the one this app was built with (Package in main.swift): a fork of the
// NullMoth driver, or another NullMoth release, named by the stick 1401 wrote. 1401 writes NullMoth/driver-source.json
// beside the package only for such a source (p1401/nullmoth.py _stage_release), after checking the package on the PC
// against its release's SHA256SUMS.txt and the source's signing key, or after the user's config allowed it unsigned.
// Here the manifest's SHA-256 only proves the stick kept those bytes; nothing on the stick can prove who is behind a
// fork, so a fork's package is installed only after a dialog that names the fork (and says so when it is unsigned).
struct DriverSourceEntry {
    let path: String
    let name: String
    let sha256: String
    let version: String
    let repo: String
    let signed: Bool
    let signature: String
    let cards: [String: String]
    var official: Bool { repo.lowercased() == DriverSource.officialRepo }
}

enum DriverSource {
    static let officialRepo = "nullmoth/nvidia-macos-driver"
    static let manifestName = "driver-source.json"

    static func matches(_ s: String, _ pattern: String) -> Bool { s.range(of: pattern, options: .regularExpression) != nil }

    /// The manifest in `dir` (a stick's NullMoth folder), or nil when it is missing or not exactly what 1401 writes.
    static func parse(_ json: Any?, dir: URL) -> DriverSourceEntry? {
        guard let j = json as? [String: Any], (j["format"] as? Int) == 1,
              let repo = j["repo"] as? String, matches(repo, #"^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$"#),
              let p = j["package"] as? [String: Any], let name = p["name"] as? String,
              matches(name, #"^nullmoth-nvidia-[0-9]+\.[0-9]+\.[0-9]+\.tar\.gz$"#),
              let sha = (p["sha256"] as? String)?.lowercased(), matches(sha, #"^[0-9a-f]{64}$"#) else { return nil }
        var cards: [String: String] = [:]
        if let ids = (j["cards"] as? [String: Any])?["ids"] as? [String: String] {
            for (k, v) in ids where matches(k, #"^[0-9A-Fa-f]{4}$"#) { cards[k.uppercased()] = v }
        }
        return DriverSourceEntry(path: dir.appendingPathComponent(name).path, name: name, sha256: sha,
                                 version: String(name.dropFirst("nullmoth-nvidia-".count).dropLast(".tar.gz".count)),
                                 repo: repo, signed: (j["signed"] as? Bool) == true, signature: j["signature"] as? String ?? "",
                                 cards: cards)
    }

    /// Every mounted volume's manifest whose package is there too.
    static func entries(volumes: String = "/Volumes") -> [DriverSourceEntry] {
        let fm = FileManager.default
        return ((try? fm.contentsOfDirectory(atPath: volumes)) ?? []).sorted().compactMap { v in
            let dir = URL(fileURLWithPath: volumes).appendingPathComponent(v).appendingPathComponent("NullMoth")
            guard let d = try? Data(contentsOf: dir.appendingPathComponent(manifestName)),
                  let e = parse(try? JSONSerialization.jsonObject(with: d), dir: dir),
                  fm.fileExists(atPath: e.path) else { return nil }
            return e
        }
    }

    static func entry(forPackage path: String, volumes: String = "/Volumes") -> DriverSourceEntry? {
        let want = URL(fileURLWithPath: path).standardizedFileURL.path
        return entries(volumes: volumes).first { URL(fileURLWithPath: $0.path).standardizedFileURL.path == want }
    }

    /// "where" in the package list: the window shows it as "Found on <where>, checksum OK."
    static func describe(_ e: DriverSourceEntry) -> String {
        e.official ? "the 1401 stick (NullMoth release \(e.version))"
            : "the 1401 stick - FORK github.com/\(e.repo), not a NullMoth release" + (e.signed ? "" : ", UNSIGNED")
    }

    /// The card table the scan uses: this app's, plus the cards a source on a stick names.
    static func merged(_ table: [String: Any], _ found: [DriverSourceEntry]) -> [String: Any] {
        var ids = table["ids"] as? [String: String] ?? [:]
        for e in found { for (k, v) in e.cards where ids[k] == nil { ids[k] = v } }
        var t = table; t["ids"] = ids
        return t
    }

    /// The confirmation shown before a fork's package is installed, or nil for a NullMoth one.
    static func warning(_ e: DriverSourceEntry) -> (title: String, text: String)? {
        guard !e.official else { return nil }
        let sig = e.signed ? "1401 checked it against the fork's own signing key (\(e.signature)) when it wrote the stick."
            : "IT IS NOT SIGNED: 1401 checked only the fork release's own SHA256SUMS.txt, which whoever controls that release can change. The driver source config allowed this."
        return ("Install a driver from a fork?",
                "\(e.name) on this stick comes from github.com/\(e.repo), a fork of the NullMoth driver, not from NullMoth. " + sig
                + " The package installs kernel extensions and runs as root. Install it only if you trust that fork.")
    }
}
