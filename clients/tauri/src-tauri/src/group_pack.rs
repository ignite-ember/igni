//! The organisation's group policy, as the desktop shell may trust it.
//!
//! The backend caches each person's group pack exactly as igni server signed
//! it — a compact JWS, `~/.igni/group-policy/pack.jws` — and applies it only
//! after verifying it (`core/config/group_policy_signature.py`). This shell
//! reads four settings itself (`air_gapped`, `update_endpoint`,
//! `update_check_ttl`, `runtime_python`) before there is a backend, so it
//! verifies the same pack the same way rather than trusting the directory it
//! sits in. It used to read an unsigned `group_settings.json`, which anybody
//! who could write the directory could edit; that file is no longer written
//! and never read.
//!
//! Checked, in this order: the JWS header (`typ`, RS256, no `crit`); the
//! signature, against a key from the JWKS cached beside the pack — only a
//! key whose RFC 7638 thumbprint is pinned, when `group_policy.trusted_keys`
//! pins any (managed file or `IGNI_GROUP_POLICY_TRUSTED_KEYS`); `token_use`;
//! `iss` against `api_url`; `sub` against the person signed in; `version`
//! not older than the newest pack the backend accepted. Anything failing is
//! no group layer at all. Without a pin the cached JWKS is only as
//! trustworthy as the directory: it stops a stray or planted file, not the
//! machine's own user. The backend, not this shell, enforces
//! `group_policy.required`.
//!
//! RS256 only: it is the only algorithm igni server signs packs with. `ring`
//! is already in the build (through rustls), so verifying here costs no new
//! crate.

use base64::engine::general_purpose::URL_SAFE_NO_PAD;
use base64::Engine;
use serde_json::{Map, Value};

/// The JWS `typ` igni server puts on a pack.
pub(crate) const PACK_TYPE: &str = "igni-group-policy+jwt";

/// The environment variable that pins keys (comma- or space-separated).
pub(crate) const TRUSTED_KEYS_ENV: &str = "IGNI_GROUP_POLICY_TRUSTED_KEYS";

/// What a pack is checked against.
#[derive(Debug, Default, Clone)]
pub(crate) struct Trust {
    pub api_url: Option<String>,
    pub subject: Option<String>,
    pub pins: Vec<String>,
    pub min_version: Option<i64>,
}

fn b64(segment: &str) -> Result<Vec<u8>, String> {
    URL_SAFE_NO_PAD.decode(segment.trim_end_matches('=')).map_err(|_| "not base64url".to_string())
}

fn json_of(segment: &str) -> Result<Value, String> {
    serde_json::from_slice(&b64(segment)?).map_err(|_| "not JSON".to_string())
}

/// RFC 7638 thumbprint of an RSA JWK — the value an administrator pins.
pub(crate) fn rsa_thumbprint(jwk: &Value) -> Option<String> {
    let e = jwk.get("e")?.as_str()?;
    let n = jwk.get("n")?.as_str()?;
    // Members in lexicographic order, no whitespace (RFC 7638 §3).
    let canonical = format!("{{\"e\":\"{e}\",\"kty\":\"RSA\",\"n\":\"{n}\"}}");
    let digest = ring::digest::digest(&ring::digest::SHA256, canonical.as_bytes());
    Some(URL_SAFE_NO_PAD.encode(digest.as_ref()))
}

/// `scheme://host[:port][/path]`, host lower-cased, no trailing slash.
pub(crate) fn normalise_issuer(url: &str) -> String {
    let url = url.trim().trim_end_matches('/');
    match url.split_once("://") {
        Some((scheme, rest)) => {
            let (host, path) = match rest.find('/') {
                Some(i) => (&rest[..i], &rest[i..]),
                None => (rest, ""),
            };
            format!("{}://{}{}", scheme.to_ascii_lowercase(), host.to_ascii_lowercase(), path)
        }
        None => url.to_string(),
    }
}

/// The policy object of a pack igni server signed for this install, or why not.
pub(crate) fn verify(compact: &str, jwks: &Value, trust: &Trust) -> Result<Value, String> {
    let parts: Vec<&str> = compact.trim().split('.').collect();
    let [head, payload, signature] = parts.as_slice() else {
        return Err("not a compact JWS".into());
    };
    let header = json_of(head).map_err(|e| format!("header {e}"))?;
    if header.get("crit").is_some() {
        return Err("critical header parameters".into());
    }
    if header.get("typ").and_then(Value::as_str) != Some(PACK_TYPE) {
        return Err("not a group pack".into());
    }
    if header.get("alg").and_then(Value::as_str) != Some("RS256") {
        return Err("not RS256".into());
    }
    let kid = header.get("kid").and_then(Value::as_str).unwrap_or("");

    let keys = jwks.get("keys").and_then(Value::as_array).cloned().unwrap_or_default();
    let candidates: Vec<&Value> = keys
        .iter()
        .filter(|k| k.get("kty").and_then(Value::as_str) == Some("RSA"))
        .filter(|k| kid.is_empty() || k.get("kid").and_then(Value::as_str) == Some(kid))
        .filter(|k| trust.pins.is_empty() || rsa_thumbprint(k).is_some_and(|t| trust.pins.contains(&t)))
        .collect();
    if candidates.is_empty() {
        return Err(if trust.pins.is_empty() { "no key to check against" } else { "no pinned key available" }.into());
    }
    let signing_input = format!("{head}.{payload}");
    let signature = b64(signature)?;
    let verified = candidates.iter().any(|key| {
        let (Some(n), Some(e)) = (
            key.get("n").and_then(Value::as_str).and_then(|n| b64(n).ok()),
            key.get("e").and_then(Value::as_str).and_then(|e| b64(e).ok()),
        ) else {
            return false;
        };
        ring::signature::RsaPublicKeyComponents { n: &n, e: &e }
            .verify(&ring::signature::RSA_PKCS1_2048_8192_SHA256, signing_input.as_bytes(), &signature)
            .is_ok()
    });
    if !verified {
        return Err("signature does not verify".into());
    }

    let claims = json_of(payload).map_err(|e| format!("payload {e}"))?;
    if claims.get("token_use").and_then(Value::as_str) != Some("group_policy") {
        return Err("not a group pack".into());
    }
    let expected = trust.api_url.as_deref().map(normalise_issuer).filter(|s| !s.is_empty()).ok_or("api_url is not set")?;
    if claims.get("iss").and_then(Value::as_str).map(normalise_issuer).as_deref() != Some(expected.as_str()) {
        return Err("issued by another server".into());
    }
    let subject = trust.subject.as_deref().filter(|s| !s.is_empty()).ok_or("nobody is signed in")?;
    if claims.get("sub").and_then(Value::as_str) != Some(subject) {
        return Err("issued for somebody else".into());
    }
    let version = claims.get("version").and_then(Value::as_i64).ok_or("no version")?;
    if trust.min_version.is_some_and(|min| version < min) {
        return Err("older than a pack already applied".into());
    }
    claims.get("policy").filter(|p| p.is_object()).cloned().ok_or_else(|| "no policy".into())
}

fn deep_merge(base: &mut Map<String, Value>, over: Map<String, Value>) {
    for (key, value) in over {
        match (base.get_mut(&key), value) {
            (Some(Value::Object(existing)), Value::Object(incoming)) => deep_merge(existing, incoming),
            (_, value) => {
                base.insert(key, value);
            }
        }
    }
}

/// The pack's enabled JSON `settings` entries, merged — what the backend's
/// `GroupPolicyPack.to_settings_dict` produces — as JSON text.
pub(crate) fn settings_json(policy: &Value) -> String {
    let mut merged = Map::new();
    for entry in policy.get("entries").and_then(Value::as_array).into_iter().flatten() {
        let is = |key: &str, want: &str| entry.get(key).and_then(Value::as_str) == Some(want);
        if !is("kind", "settings") || !is("content_type", "json") {
            continue;
        }
        if entry.get("enabled").and_then(Value::as_bool) == Some(false) {
            continue;
        }
        if let Some(Ok(Value::Object(parsed))) =
            entry.get("content").and_then(Value::as_str).map(serde_json::from_str::<Value>)
        {
            deep_merge(&mut merged, parsed);
        }
    }
    Value::Object(merged).to_string()
}

/// `sub` of the session token in a credentials file's text. Decoded, not
/// verified: it names whose pack may apply, and a pack for anybody else
/// would still need igni server's signature.
pub(crate) fn subject_from_credentials(text: &str) -> Option<String> {
    let creds: Value = serde_json::from_str(text).ok()?;
    let token = creds.get("access_token")?.as_str()?;
    let claims = json_of(token.split('.').nth(1)?).ok()?;
    claims.get("sub")?.as_str().map(str::to_string)
}

/// Pins from `IGNI_GROUP_POLICY_TRUSTED_KEYS`.
pub(crate) fn pins_from_env(value: Option<&str>) -> Vec<String> {
    value
        .unwrap_or("")
        .split(|c: char| c == ',' || c.is_whitespace())
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .collect()
}

/// Pins from a managed-settings file: `group_policy.trusted_keys`, as JSON or
/// as the block YAML an administrator writes (a flow list `[a, b]` or one
/// `- item` per line).
pub(crate) fn pins_from_managed(text: &str) -> Vec<String> {
    if text.trim_start().starts_with('{') {
        let Ok(value) = serde_json::from_str::<Value>(text) else { return Vec::new() };
        return value
            .pointer("/group_policy/trusted_keys")
            .and_then(Value::as_array)
            .map(|items| items.iter().filter_map(Value::as_str).map(str::to_string).collect())
            .unwrap_or_default();
    }
    let clean = |s: &str| s.split('#').next().unwrap_or("").trim().trim_matches('"').trim_matches('\'').to_string();
    let mut lines = text.lines().skip_while(|line| !line.starts_with("group_policy:"));
    if lines.next().is_none() {
        return Vec::new();
    }
    let block: Vec<&str> = lines.take_while(|line| line.trim().is_empty() || line.starts_with(char::is_whitespace)).collect();
    let Some(at) = block.iter().position(|line| line.trim_start().starts_with("trusted_keys:")) else {
        return Vec::new();
    };
    let key_line = block[at];
    let indent = key_line.len() - key_line.trim_start().len();
    let rest = key_line.trim_start()["trusted_keys:".len()..].trim();
    if rest.starts_with('[') {
        return rest
            .trim_start_matches('[')
            .split(']')
            .next()
            .unwrap_or("")
            .split(',')
            .map(clean)
            .filter(|s| !s.is_empty())
            .collect();
    }
    block[at + 1..]
        .iter()
        .take_while(|line| line.trim().is_empty() || line.len() - line.trim_start().len() > indent || line.trim_start().starts_with("- "))
        .filter_map(|line| line.trim_start().strip_prefix("- ").map(clean))
        .filter(|s| !s.is_empty())
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use ring::rand::SystemRandom;
    use ring::signature::{RsaKeyPair, RSA_PKCS1_SHA256};

    // A 2048-bit RSA key, PKCS#8 DER in standard base64, generated once for
    // these tests: `ring` verifies and signs RSA but does not generate keys.
    const TEST_KEY_PKCS8: &[&str] = &[
        "MIIEvwIBADANBgkqhkiG9w0BAQEFAASCBKkwggSlAgEAAoIBAQCo/LvU3Ep/4uyB1PWRGb2TAMONPlJOJo0bAVO7QOvWjGL5",
        "5upnqG+fJfrMPRjeLjH4qwNMnc39zmMz5OLcT8SrHLyREM4IwhpRtcFzOiRZrR9TAgoz0EDvshfQoCy7h/YSS+QQYWKVt0R6",
        "W3MKrMupvS0byCRoS5jLiDj+/f72YL/XnhWfDxmsa/pkRQcjCAKuwUNFFgczM9JWyYAT2GeKYwb4WIOivVCjQd+gnbRLewJy",
        "xMQ6uZstk1GBAKKGOOcAF2/U7E4lQG7jMCNGWw4LfmMkVyDQjgFmLsA9xdXSYWrIB+8vD2G3d/JM8Mk9QUAZzoH5f5QxB5fH",
        "vt0Eye57AgMBAAECggEABif64cTKtAD807gB4DDbXqUa+qTIE8/EuTShq8MwNEc1/dujM3aH5njYO6OIKePt7xyvyJVBIVto",
        "8sWGXDdNt1wbJH/Dub70fYCNFRkLpPaxGvGKlJukGfdXdcK4DnbkCIOaQdyCXmjoRjrZpIxp+PuKzqXKacBDRzrltKhlJu2N",
        "NMhw1tkkKxzzUJ8EnaTmRsIvjUZxVZBH1k3n0g6NMnWCDHfWDbhkEutGRI1NLHH9QWqD2C1KiGTWUAsBdsvHDycDii0icQKp",
        "hTNud05fxnzk63aCGeYIF20XMPodPnZqETfxWor9IQ6iUlC7yQtZdkBL0O9gflrMlYTE42kh9QKBgQDYuGEamt42gHm+wlx7",
        "J8Zylrn18jLyT5HQnfnFrieVg176CTcDguUcclbtitMLhN++Sy+9tfDJzkkb1lNTedNnnqraaQyw7QrFujHgmnrN46S7IdHu",
        "Za0Yw+gJPAt/6Q+t6SzjsWspffuA+rhb9K0pFfzfkRXJUksZy0r25GaA9QKBgQDHnZZW7lGyTafhSaBb36TcorpxJCMwxF1J",
        "O2K4aueji09Eo98EwX4MsvHJu8TKlDF3lEeHAebRg2KUycqwDghVn7bRBKaOyEFmDUfaQyJYniesJ0CsV6vHnPL1nnVQAXtI",
        "yPsLOWjaISvgQEEZ8JX3Nj2aV1gAMJJAgxbx2TBLrwKBgQDC5f4d6QzSQvQ6A50b/IdRYg/XCXubwsdFr+yCRL7AjSaLAmKB",
        "O+RDkuhuNBzcDSESh5dlqwvi0SuaZ0MEnVjrpRvN1A9fxa2caHiWnxa1tnXc/OtI1BO5Y+V2+JL282fwZPTDyDixVeUq9095",
        "Ey/u9SE3BclsBiAYEGVYZ8tvSQKBgQCvGX0XJFZlodjxtjQEY1mqcNQ7CwgTbyylYIJoYt5uh3UxMM4gzdpUKWQPIbauPPBn",
        "hJp5wyFGB4oiGiDB7o1BDqi/EOVJukAj1OKCgwFN2u3SBVyl0QHOR86XxjgBxkruvd35KpLfRNnIDJUOvAvQnrPK/3MiN3kT",
        "PeqSm4HR4QKBgQCnYGtBw/eJCiBdZ04o4xHY7YuYRUrLcKkNxBj6CvBJ6wQqcEAA0lP4yY/ykSzIB2SwHHOLUOTOnHyUUqCQ",
        "P+UuPoyclBOsrCp8VAvoRcHmvjYAg8YKjw6eUg5z4HdNextguv3E79OcISeaOWkeJ6SptyCVtEVm6AjOk2nrTNL80w==",
    ];

    fn key() -> RsaKeyPair {
        let der = base64::engine::general_purpose::STANDARD.decode(TEST_KEY_PKCS8.concat()).unwrap();
        RsaKeyPair::from_pkcs8(&der).expect("test key")
    }

    fn jwks(pair: &RsaKeyPair) -> Value {
        let components = ring::rsa::PublicKeyComponents::<Vec<u8>>::from(pair.public());
        let (n, e) = (components.n, components.e);
        let mut jwk = serde_json::json!({"kty": "RSA", "n": URL_SAFE_NO_PAD.encode(n), "e": URL_SAFE_NO_PAD.encode(e)});
        let kid = rsa_thumbprint(&jwk).unwrap();
        jwk["kid"] = Value::String(kid);
        serde_json::json!({"keys": [jwk]})
    }

    fn sign(pair: &RsaKeyPair, kid: &str, claims: &Value) -> String {
        let head = URL_SAFE_NO_PAD.encode(serde_json::json!({"alg": "RS256", "kid": kid, "typ": PACK_TYPE}).to_string());
        let body = URL_SAFE_NO_PAD.encode(claims.to_string());
        let input = format!("{head}.{body}");
        let mut signature = vec![0; pair.public().modulus_len()];
        pair.sign(&RSA_PKCS1_SHA256, &SystemRandom::new(), input.as_bytes(), &mut signature).unwrap();
        format!("{input}.{}", URL_SAFE_NO_PAD.encode(signature))
    }

    fn claims(sub: &str, iss: &str, version: i64) -> Value {
        serde_json::json!({
            "iss": iss, "sub": sub, "org": "o", "iat": 1, "version": version,
            "token_use": "group_policy",
            "policy": {"entries": [{
                "kind": "settings", "entry_name": "p", "content_type": "json", "enabled": true,
                "content": "{\"air_gapped\": true, \"update_endpoint\": \"https://m.internal/l.json\"}"
            }]}
        })
    }

    fn trust() -> Trust {
        Trust { api_url: Some("https://igni.example.invalid/".into()), subject: Some("p1".into()), ..Trust::default() }
    }

    #[test]
    fn a_genuine_pack_verifies_and_yields_its_settings() {
        let pair = key();
        let keys = jwks(&pair);
        let kid = keys["keys"][0]["kid"].as_str().unwrap().to_string();
        let compact = sign(&pair, &kid, &claims("p1", "https://igni.example.invalid", 5));
        let policy = verify(&compact, &keys, &trust()).expect("verifies");
        let settings = crate::desktop_settings::DesktopSettings::from_json(&settings_json(&policy));
        assert_eq!(settings.air_gapped, Some(true));
        assert_eq!(settings.update_endpoint.as_deref(), Some("https://m.internal/l.json"));
    }

    #[test]
    fn a_forged_or_misdirected_pack_does_not() {
        let pair = key();
        let keys = jwks(&pair);
        let kid = keys["keys"][0]["kid"].as_str().unwrap().to_string();
        let good = sign(&pair, &kid, &claims("p1", "https://igni.example.invalid", 5));

        // Edited payload, original signature.
        let parts: Vec<&str> = good.split('.').collect();
        let mut edited = claims("p1", "https://igni.example.invalid", 5);
        edited["policy"]["entries"] = Value::Array(vec![]);
        let forged = format!("{}.{}.{}", parts[0], URL_SAFE_NO_PAD.encode(edited.to_string()), parts[2]);
        assert!(verify(&forged, &keys, &trust()).is_err(), "edited");

        // Another person, another server, a rollback, nobody signed in.
        assert!(verify(&sign(&pair, &kid, &claims("p2", "https://igni.example.invalid", 5)), &keys, &trust()).is_err());
        assert!(verify(&sign(&pair, &kid, &claims("p1", "https://other.invalid", 5)), &keys, &trust()).is_err());
        assert!(verify(&good, &keys, &Trust { min_version: Some(6), ..trust() }).is_err(), "rollback");
        assert!(verify(&good, &keys, &Trust { subject: None, ..trust() }).is_err());

        // Unsigned, or the plain JSON the cache used to hold.
        assert!(verify("{\"air_gapped\": false}", &keys, &trust()).is_err());
        assert!(verify(&format!("{}.{}.", parts[0], parts[1]), &keys, &trust()).is_err());

        // A pin that is not this key refuses it, though the JWKS holds it.
        assert!(verify(&good, &keys, &Trust { pins: vec!["not-it".into()], ..trust() }).is_err());
        assert!(verify(&good, &keys, &Trust { pins: vec![kid.clone()], ..trust() }).is_ok());
    }

    #[test]
    fn pins_are_read_from_the_managed_file_and_the_environment() {
        assert_eq!(pins_from_managed("group_policy:\n  trusted_keys: [abc, \"def\"]\n"), vec!["abc", "def"]);
        assert_eq!(pins_from_managed("x: 1\ngroup_policy:\n  required: true\n  trusted_keys:\n    - abc\n    - def # old\nother: 2\n"), vec!["abc", "def"]);
        assert_eq!(pins_from_managed("{\"group_policy\": {\"trusted_keys\": [\"abc\"]}}"), vec!["abc"]);
        assert!(pins_from_managed("trusted_keys: [abc]\n").is_empty(), "top-level is not group_policy");
        assert_eq!(pins_from_env(Some("abc, def ghi")), vec!["abc", "def", "ghi"]);
    }

    #[test]
    fn the_subject_comes_from_the_session_token() {
        let body = URL_SAFE_NO_PAD.encode("{\"sub\":\"p1\"}");
        let creds = format!("{{\"access_token\": \"h.{body}.s\"}}");
        assert_eq!(subject_from_credentials(&creds).as_deref(), Some("p1"));
        assert_eq!(subject_from_credentials("{}"), None);
    }
}
