// Copyright (c) 2026 Adam Souzis
// SPDX-License-Identifier: MIT
//! Source-tracking YAML node tree and loader.

use crate::Error;
use indexmap::IndexMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

/// Provenance for a [`Node::Mapping`]: which file the mapping was
/// originally loaded from.
///
/// Used by include resolution to compute directories for relative
/// `+include:` paths and (later) by diagnostics to point users at
/// the right source location.
#[derive(Clone, Debug)]
pub struct Source {
    /// Path to the source file.
    pub file: Arc<PathBuf>,
}

impl Source {
    /// Directory containing [`file`](Self::file). Falls back to `.`
    /// when the path has no parent (e.g. a bare filename).
    pub fn base_dir(&self) -> &Path {
        self.file.parent().unwrap_or_else(|| Path::new("."))
    }
}

/// A YAML node carrying per-mapping source provenance.
///
/// Mirrors [`serde_json::Value`] but attaches an [`Arc<Source>`] to
/// every mapping. Sequences and scalars are not annotated; they
/// inherit their enclosing mapping's source by lookup.
#[derive(Clone, Debug)]
pub enum Node {
    Null,
    Bool(bool),
    Number(serde_json::Number),
    String(String),
    Sequence(Vec<Node>),
    Mapping {
        entries: IndexMap<String, Node>,
        source: Arc<Source>,
    },
}

impl Node {
    /// Convert to [`serde_json::Value`], discarding source info.
    ///
    /// Storage and existing crate APIs operate on `Value`; this is
    /// the boundary conversion at write time.
    pub fn to_json_value(&self) -> serde_json::Value {
        use serde_json::Value;
        match self {
            Node::Null => Value::Null,
            Node::Bool(b) => Value::Bool(*b),
            Node::Number(n) => Value::Number(n.clone()),
            Node::String(s) => Value::String(s.clone()),
            Node::Sequence(items) => Value::Array(items.iter().map(Self::to_json_value).collect()),
            Node::Mapping { entries, .. } => {
                let mut obj = serde_json::Map::new();
                for (k, v) in entries {
                    obj.insert(k.clone(), v.to_json_value());
                }
                Value::Object(obj)
            }
        }
    }
}

/// Load a YAML file into a [`Node`] tree, attaching [`Source`] to
/// every mapping.
///
/// Multi-document YAML files take the first document; an empty file
/// loads as [`Node::Null`].
pub fn load_file(path: &Path) -> Result<Node, Error> {
    let text = std::fs::read_to_string(path)?;
    let source = Arc::new(Source {
        file: Arc::new(path.to_path_buf()),
    });
    load_str(&text, &source, path)
}

fn load_str(text: &str, source: &Arc<Source>, path: &Path) -> Result<Node, Error> {
    use saphyr::LoadableYamlNode;
    let mut docs = saphyr::Yaml::load_from_str(text).map_err(|e| Error::Yaml {
        path: path.display().to_string(),
        message: e.to_string(),
    })?;
    if docs.is_empty() {
        return Ok(Node::Null);
    }
    from_saphyr(docs.remove(0), source, path)
}

fn from_saphyr(y: saphyr::Yaml<'_>, source: &Arc<Source>, path: &Path) -> Result<Node, Error> {
    match y {
        saphyr::Yaml::Value(scalar) => scalar_to_node(scalar, path),
        saphyr::Yaml::Representation(cow, _style, _tag) => {
            // `early_parse` defaults on, so this branch should be rare
            // in practice; treat the raw text as a string scalar.
            Ok(Node::String(cow.into_owned()))
        }
        saphyr::Yaml::Sequence(items) => {
            let mut out = Vec::with_capacity(items.len());
            for item in items {
                out.push(from_saphyr(item, source, path)?);
            }
            Ok(Node::Sequence(out))
        }
        saphyr::Yaml::Mapping(map) => {
            let mut entries = IndexMap::with_capacity(map.len());
            for (k, v) in map {
                let key = scalar_key(k, path)?;
                entries.insert(key, from_saphyr(v, source, path)?);
            }
            Ok(Node::Mapping {
                entries,
                source: source.clone(),
            })
        }
        saphyr::Yaml::Tagged(_tag, inner) => from_saphyr(*inner, source, path),
        saphyr::Yaml::Alias(_) => Err(Error::Yaml {
            path: path.display().to_string(),
            message: "unresolved YAML alias (anchors not supported)".into(),
        }),
        saphyr::Yaml::BadValue => Err(Error::Yaml {
            path: path.display().to_string(),
            message: "invalid YAML value".into(),
        }),
    }
}

fn scalar_to_node(scalar: saphyr::Scalar<'_>, path: &Path) -> Result<Node, Error> {
    match scalar {
        saphyr::Scalar::Null => Ok(Node::Null),
        saphyr::Scalar::Boolean(b) => Ok(Node::Bool(b)),
        saphyr::Scalar::Integer(i) => Ok(Node::Number(i.into())),
        saphyr::Scalar::FloatingPoint(of) => serde_json::Number::from_f64(of.0)
            .map(Node::Number)
            .ok_or_else(|| Error::Yaml {
                path: path.display().to_string(),
                message: format!("non-finite float: {}", of.0),
            }),
        saphyr::Scalar::String(s) => Ok(Node::String(s.into_owned())),
    }
}

fn scalar_key(y: saphyr::Yaml<'_>, path: &Path) -> Result<String, Error> {
    let make_err = |msg: String| Error::Yaml {
        path: path.display().to_string(),
        message: msg,
    };
    match y {
        saphyr::Yaml::Value(saphyr::Scalar::String(s)) => Ok(s.into_owned()),
        saphyr::Yaml::Value(saphyr::Scalar::Integer(i)) => Ok(i.to_string()),
        saphyr::Yaml::Value(saphyr::Scalar::Boolean(b)) => Ok(b.to_string()),
        saphyr::Yaml::Value(saphyr::Scalar::Null) => Ok(String::new()),
        saphyr::Yaml::Value(saphyr::Scalar::FloatingPoint(of)) => Ok(of.0.to_string()),
        saphyr::Yaml::Representation(cow, _, _) => Ok(cow.into_owned()),
        other => Err(make_err(format!(
            "non-scalar mapping key not supported: {other:?}"
        ))),
    }
}
