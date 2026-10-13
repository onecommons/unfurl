// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! A commit's object id.

use std::fmt;
use std::str::FromStr;

use crate::error::{Error, Result};

/// A commit's object id: a commit, not a blob, tree or ref name. Written
/// as hex in the database, in JSON and in commit messages.
#[derive(Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct CommitId(gix::ObjectId);

impl CommitId {
    /// The id as gix takes it.
    pub fn oid(&self) -> gix::ObjectId {
        self.0
    }

    /// The first `n` hex digits.
    pub fn short(&self, n: usize) -> String {
        let hex = self.to_string();
        hex[..n.min(hex.len())].to_string()
    }
}

impl From<gix::ObjectId> for CommitId {
    fn from(oid: gix::ObjectId) -> Self {
        CommitId(oid)
    }
}

impl FromStr for CommitId {
    type Err = Error;

    /// A full hex object id; anything else, a branch name or `HEAD`
    /// included, is an error.
    fn from_str(s: &str) -> Result<Self> {
        gix::ObjectId::from_hex(s.as_bytes())
            .map(CommitId)
            .map_err(|e| Error::Git(format!("{s:?} isn't a commit id: {e}")))
    }
}

impl TryFrom<&str> for CommitId {
    type Error = Error;

    fn try_from(s: &str) -> Result<Self> {
        s.parse()
    }
}

impl fmt::Display for CommitId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl fmt::Debug for CommitId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}", self.0)
    }
}

impl serde::Serialize for CommitId {
    fn serialize<S: serde::Serializer>(&self, s: S) -> std::result::Result<S::Ok, S::Error> {
        s.collect_str(self)
    }
}

impl<'de> serde::Deserialize<'de> for CommitId {
    fn deserialize<D: serde::Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
        let s = <std::borrow::Cow<'de, str>>::deserialize(d)?;
        s.parse().map_err(serde::de::Error::custom)
    }
}

impl<DB: sqlx::Database> sqlx::Type<DB> for CommitId
where
    String: sqlx::Type<DB>,
{
    fn type_info() -> DB::TypeInfo {
        <String as sqlx::Type<DB>>::type_info()
    }

    fn compatible(ty: &DB::TypeInfo) -> bool {
        <String as sqlx::Type<DB>>::compatible(ty)
    }
}

impl<'q, DB: sqlx::Database> sqlx::Encode<'q, DB> for CommitId
where
    String: sqlx::Encode<'q, DB>,
{
    fn encode_by_ref(
        &self,
        buf: &mut <DB as sqlx::Database>::ArgumentBuffer<'q>,
    ) -> std::result::Result<sqlx::encode::IsNull, sqlx::error::BoxDynError> {
        self.to_string().encode(buf)
    }
}

impl<'r, DB: sqlx::Database> sqlx::Decode<'r, DB> for CommitId
where
    String: sqlx::Decode<'r, DB>,
{
    fn decode(
        value: <DB as sqlx::Database>::ValueRef<'r>,
    ) -> std::result::Result<Self, sqlx::error::BoxDynError> {
        Ok(<String as sqlx::Decode<'r, DB>>::decode(value)?.parse()?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const HEX: &str = "4551885dfab39991cfdb958cb79fcb6aa282481d";

    #[test]
    fn a_hex_id_round_trips() {
        let c: CommitId = HEX.parse().expect("hex");
        assert_eq!(c.to_string(), HEX);
        assert_eq!(
            serde_json::to_string(&c).expect("json"),
            format!("\"{HEX}\"")
        );
        let back: CommitId = serde_json::from_str(&format!("\"{HEX}\"")).expect("json");
        assert_eq!(back, c);
        assert_eq!(c.short(7), &HEX[..7]);
    }

    #[test]
    fn a_name_isnt_a_commit() {
        for s in ["main", "HEAD", "4551885", ""] {
            assert!(s.parse::<CommitId>().is_err(), "{s:?}");
        }
    }
}
